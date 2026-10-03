"""OPTIMIZER: decides where this wallet's USDG and stock earn the most pre-season points, and deposits.

Points model (Issuance stream, 30% of the weekly pool): matched notional x days x barrier tier.
- At strike a series matches min(COUPON, SHIELD value); every COUPON gets matched / COUPON of its
  deposit, every SHIELD matched / SHIELD of its value (pro rata). So the wallet's matched notional
  in a series is  myCoupon / D * min(D, S)  +  myShield / S * min(D, S).
- Tier: barrier -30% Comfortable 1.0x, -15% Tight 1.8x, -7% Aggressive 3.0x. A breach on Tight or
  Aggressive zeroes the note, so those are weighted by the chance the barrier holds at every
  observation (driftless lognormal price, yearly volatility per stock from config.OPT_VOLATILITY).
- Days: from strike to the last observation, capped at config.OPT_HORIZON_DAYS.

Every option is scored as points per USDG spent: a COUPON costs its amount, a SHIELD costs only its
prefund (the stock is the wallet's own). The budget (90% of the USDG) goes out in chunks: first one
chunk per stock the wallet holds (so every stock is used), then each chunk to the best option,
re-scoring after each chunk (returns fall as a side fills up). No series takes more than
config.OPT_MAX_SERIES_SHARE of the budget. Chunks are merged into one deposit per series and side.
"""
import math
import random
import time

from eth_abi import decode, encode

import config
from deposit import CORE, DEPOSITS, ERC20_EXTRA, GET_SERIES, SERIES_VIEW, open_series, send
from faucet import ABI as ERC20_ABI, TOKENS
from register import FILE_LOCK, log
from shield import HEADROOM, SHIELDS, approve_if_needed

TIERS = ((7000, "Comfortable", 1.0), (8500, "Tight", 1.8), (10_000, "Aggressive", 3.0))  # barrierBps <= limit
CORE_ABI = [
    {"type": "function", "name": "depositCoupon", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}], "outputs": []},
    {"type": "function", "name": "depositShield", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"}], "outputs": []},
    {"type": "function", "name": "requiredPrefund", "stateMutability": "view",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "getPosition", "stateMutability": "view",
     "inputs": [{"type": "uint256"}, {"type": "address"}],
     "outputs": [{"type": "tuple", "components": [{"type": "uint256"}] * 8 + [{"type": "bool"}]}]},
]
_terms = {}  # series id -> (barrierBps, observation timestamps): fixed at creation


def tier(barrier_bps):
    return next((name, mult) for limit, name, mult in TIERS if barrier_bps <= limit)


def survival(symbol, barrier_bps, observations):
    """Chance the price stays above the barrier at every observation after the strike."""
    sigma = config.OPT_VOLATILITY.get(symbol, 0.5)
    b, start, p = barrier_bps / 10_000, observations[0], 1.0
    for ts in observations[1:]:
        t = max(ts - start, 3600) / (365 * 86400)
        z = (math.log(b) + sigma * sigma * t / 2) / (sigma * math.sqrt(t))
        p *= 1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return p


def weight(s, barrier_bps, observations):
    """Expected points per USDG of matched notional: tier x survival x days. A Tight/Aggressive series
    whose barrier holds with less than config.OPT_MIN_KEEP gets weight 0: a breach there zeroes the
    note's points, so it is not bought at all (a Comfortable breach keeps the points)."""
    name, mult = tier(barrier_bps)
    keep = 1.0 if name == "Comfortable" else survival(s["symbol"], barrier_bps, observations)
    days = min((observations[-1] - observations[0]) / 86400, config.OPT_HORIZON_DAYS)
    if keep < config.OPT_MIN_KEEP:
        return 0.0, name, keep
    return mult * keep * days, name, keep


def matched(my_c, my_s, d, s):
    m = min(d, s)
    return (my_c / d * m if d > 0 else 0.0) + (my_s / s * m if s > 0 else 0.0)


def plan(state, cash, stock_value, chunk):
    """Greedy split of `cash` USDG. state: list of dicts with floats in USDG. Returns {(sid, side): usdg}."""
    alloc, spent_in = {}, {st["id"]: 0.0 for st in state}
    cap = cash * config.OPT_MAX_SERIES_SHARE

    def options(only=None):
        for st in state:
            if only and st["symbol"] != only or spent_in[st["id"]] + chunk > cap:
                continue
            before = matched(st["myC"], st["myS"], st["D"], st["S"])
            if st["room"] >= chunk:  # COUPON: costs its amount
                gain = matched(st["myC"] + chunk, st["myS"], st["D"] + chunk, st["S"]) - before
                yield gain * st["w"] / chunk, st, "coupon", chunk, chunk
            value = min(chunk / st["prefund"], stock_value.get(st["symbol"], 0.0))  # SHIELD: costs its prefund
            if value >= st["price"]:
                gain = matched(st["myC"], st["myS"] + value, st["D"], st["S"] + value) - before
                yield gain * st["w"] / (value * st["prefund"]), st, "shield", value * st["prefund"], value

    def take(opt):
        nonlocal cash
        _, st, side, cost, size = opt
        if side == "coupon":
            st["myC"] += size; st["D"] += size; st["room"] -= size
        else:
            st["myS"] += size; st["S"] += size; stock_value[st["symbol"]] -= size
        alloc[(st["id"], side)] = alloc.get((st["id"], side), 0.0) + size
        spent_in[st["id"]] += cost
        cash -= cost

    for symbol in random.sample(sorted({st["symbol"] for st in state}), len({st["symbol"] for st in state})):
        best = max(options(symbol), key=lambda o: o[0], default=None)  # one chunk per stock first
        if best and best[0] > 0 and cash >= best[3]:
            take(best)
    while cash >= chunk:
        best = max(options(), key=lambda o: o[0], default=None)
        if not best or best[0] <= 0:
            break
        take(best)
    return alloc


def optimize(w3, line_no, acct, tx_delay, dry=False):
    """Analyse the wallet and the open series, plan, deposit (dry: only print the plan).
    Returns True when something was sent."""
    core = w3.eth.contract(address=CORE, abi=CORE_ABI)
    usdg = w3.eth.contract(address=TOKENS["USDG"], abi=ERC20_ABI + ERC20_EXTRA)
    series = open_series(w3)
    cash = usdg.functions.balanceOf(acct.address).call() * 9 // 10
    stocks = {sym: w3.eth.contract(address=TOKENS[sym], abi=ERC20_ABI + ERC20_EXTRA)
              for sym in {s["symbol"] for s in series} if sym in TOKENS}
    held = {sym: c.functions.balanceOf(acct.address).call() for sym, c in stocks.items()}
    if cash < config.OPT_MIN_CHUNK * 10**6:
        log(f"#{line_no} {acct.address}: optimizer skip, no idle USDG")
        return False

    state, by_id = [], {s["id"]: s for s in series}
    for s in series:
        if s["id"] not in _terms:
            v = decode([SERIES_VIEW], w3.eth.call({"to": CORE, "data": GET_SERIES + encode(["uint256"], [s["id"]])}))[0]
            _terms[s["id"]] = (v[3], list(v[12]))
        barrier, observations = _terms[s["id"]]
        w, tier_name, keep = weight(s, barrier, observations)
        pos = core.functions.getPosition(s["id"], acct.address).call()
        price = s["price"] / 1e6
        state.append({"id": s["id"], "symbol": s["symbol"], "w": w, "tier": tier_name, "keep": keep,
                      "price": price, "prefund": s["per_share"] / s["price"] * HEADROOM,
                      "D": s["D"] / 1e6, "S": s["S_stock"] * s["price"] / 1e24, "room": s["room"] / 1e6,
                      "myC": pos[0] / 1e6, "myS": pos[1] * s["price"] / 1e24})
    stock_value = {sym: held[sym] / 1e18 * next(st["price"] for st in state if st["symbol"] == sym)
                   for sym in held if any(st["symbol"] == sym for st in state)}
    budget = cash / 1e6
    chunk = max(config.OPT_MIN_CHUNK, budget / config.OPT_CHUNKS)
    alloc = plan(state, budget, stock_value, chunk)
    if not alloc:
        log(f"#{line_no} {acct.address}: optimizer, nothing worth depositing")
        return False
    if dry:
        print(f"budget {budget:,.0f} USDG, chunk {chunk:,.0f}; stock value "
              + ", ".join(f"{k} {v:,.0f}" for k, v in sorted(stock_value.items())))
        print(f"{'id':>3} {'sym':5} {'tier':11} {'keep':>5} {'weight':>7} {'COUPON D':>10} {'SHIELD S':>10} "
              f"{'mine C':>8} {'mine S':>9}  plan")
        for st in sorted(state, key=lambda x: -x["w"]):
            p = ", ".join(f"{side} {size:,.0f}" for (sid, side), size in alloc.items() if sid == st["id"])
            print(f"{st['id']:>3} {st['symbol']:5} {st['tier']:11} {st['keep']:>5.2f} {st['w']:>7.1f} {st['D']:>10,.0f} "
                  f"{st['S']:>10,.0f} {st['myC']:>8,.0f} {st['myS']:>9,.0f}  {p}")
        return False

    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    made = []
    for (sid, side), size in random.sample(list(alloc.items()), len(alloc)):
        s, st = by_id[sid], next(x for x in state if x["id"] == sid)
        tag = f"#{sid} {s['symbol']} {st['tier'][0]}"
        try:
            if side == "coupon":
                amount = int(size * 100) * 10_000  # USDG, 2 decimals
                if amount < s["min"] or amount > usdg.functions.balanceOf(acct.address).call():
                    continue
                fn = core.functions.depositCoupon(sid, amount)
                nonce = approve_if_needed(w3, acct, usdg, amount, nonce, tx_delay)
                h = send(w3, acct, fn, nonce)
                nonce += 1
                s["D"] += amount
                with FILE_LOCK, DEPOSITS.open("a", encoding="utf-8") as f:
                    f.write(f"{acct.address},{sid},{amount / 1e6},{h}\n")
                made.append(f"{tag} COUPON {amount / 1e6:,.0f}")
            else:
                shares = min(int(size / st["price"]), held[s["symbol"]] // 10**18)
                if shares < 1:
                    continue
                amount = shares * 10**18
                prefund = int(core.functions.requiredPrefund(sid, amount).call() * HEADROOM) + 1
                fn = core.functions.depositShield(sid, amount, prefund)
                nonce = approve_if_needed(w3, acct, stocks[s["symbol"]], amount, nonce, tx_delay)
                nonce = approve_if_needed(w3, acct, usdg, prefund, nonce, tx_delay)
                try:
                    fn.estimate_gas({"from": acct.address})
                except Exception as e:
                    log(f"#{line_no} {acct.address}: {tag} SHIELD refused: {str(e)[:120]}")
                    continue
                h = send(w3, acct, fn, nonce)
                nonce += 1
                s["S_stock"] += amount
                held[s["symbol"]] -= amount
                with FILE_LOCK, SHIELDS.open("a", encoding="utf-8") as f:
                    f.write(f"{acct.address},{sid},{shares},{prefund / 1e6},{h}\n")
                made.append(f"{tag} SHIELD {shares} sh (~{shares * st['price']:,.0f}) +{prefund / 1e6:,.0f} prefund")
        except Exception as e:
            log(f"#{line_no} {acct.address}: {tag} {side} ERROR {str(e)[:200]}")
            nonce = w3.eth.get_transaction_count(acct.address, "pending")
        time.sleep(random.uniform(*tx_delay))

    left = usdg.functions.balanceOf(acct.address).call() / 1e6
    log(f"#{line_no} {acct.address}: optimizer [{', '.join(made) or '-'}], USDG left {left:,.2f}")
    return bool(made)


if __name__ == "__main__":  # python optimizer.py <line>: print the plan for that account, send nothing
    import sys

    from eth_account import Account

    from register import ACCOUNTS, parse, read_lines
    from rpc import make_w3

    Account.enable_unaudited_hdwallet_features()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    optimize(make_w3(), n, Account.from_mnemonic(parse(read_lines(ACCOUNTS)[n - 1])[0]), (0, 0), dry=True)
