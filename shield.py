"""SHIELD deposits (stock + USDG prefund) into open Note Systems series. Used by main.py.

Fully automatic: every stock token the account holds goes into one SHIELD position per run,
in the open series with the most free COUPON demand, sized so the position is fully matched
(SHIELD only matches against COUPON deposits; the excess would sit unmatched until strike).
90% of the USDG balance is split evenly across the stocks for the prefund
(NoteCore.requiredPrefund + 0.1%, like the app). Each deposit is simulated first.
"""
import random
import time
from pathlib import Path

from deposit import CORE, DEPOSITS, ERC20_EXTRA, coupon_free, open_series, send
from faucet import ABI as ERC20_ABI, TOKENS
from register import FILE_LOCK, log, read_lines

BASE = Path(__file__).resolve().parent
SHIELDS = BASE / "shields.txt"
HEADROOM = 1.001  # the app adds 0.1% so the deposit passes if the price ticks up before inclusion

CORE_ABI = [
    {"type": "function", "name": "requiredPrefund", "stateMutability": "view",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "depositShield", "stateMutability": "nonpayable",
     "inputs": [{"name": "seriesId", "type": "uint256"}, {"name": "stockAmount", "type": "uint256"},
                {"name": "prefundQuote", "type": "uint256"}], "outputs": []},
]


def active_shields(address, open_ids):
    ids = set()
    for line in read_lines(SHIELDS):
        addr, sid = line.split(",")[:2]
        if addr == address and int(sid) in open_ids:
            ids.add(int(sid))
    return ids


def approve_if_needed(w3, acct, token, amount, nonce, tx_delay):
    if token.functions.allowance(acct.address, CORE).call() >= amount:
        return nonce
    send(w3, acct, token.functions.approve(CORE, amount), nonce)
    time.sleep(random.uniform(*tx_delay))
    return nonce + 1


def shield_all(w3, line_no, acct, tx_delay):
    """SHIELD every stock the account holds, sized so each position is fully matched.

    Per stock: the open series with the most free COUPON demand (D minus SHIELD already
    posted), series this account doesn't shield yet first. Shares = min(held, what that free
    demand matches, what the stock's share of 90% of the USDG balance can prefund).
    Returns True when at least one position was made.
    """
    core = w3.eth.contract(address=CORE, abi=CORE_ABI)
    usdg = w3.eth.contract(address=TOKENS["USDG"], abi=ERC20_ABI + ERC20_EXTRA)
    series = open_series(w3)
    taken = active_shields(acct.address, {s["id"] for s in series})

    stock = {sym: w3.eth.contract(address=TOKENS[sym], abi=ERC20_ABI + ERC20_EXTRA)
             for sym in {s["symbol"] for s in series} if sym in TOKENS}
    held = {sym: c.functions.balanceOf(acct.address).call() for sym, c in stock.items()}
    held = {sym: b for sym, b in held.items() if b >= 10**18}
    if not held:
        log(f"#{line_no} {acct.address}: shield skip, no stock tokens")
        return False
    budget = usdg.functions.balanceOf(acct.address).call() * 9 // 10 // len(held)  # USDG per stock
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    made, no_demand = [], []

    for sym in random.sample(list(held), len(held)):
        options = []
        for s in [s for s in series if s["symbol"] == sym]:
            free = coupon_free(s)  # COUPON demand not yet covered by SHIELD
            if free >= s["price"]:
                options.append(((s["id"] not in taken, free), s, free))
        if not options:
            no_demand.append(sym)
            continue
        # random pick among the 3 best series, so wallets don't all land in the same one (anti-sybil)
        options.sort(key=lambda o: o[0], reverse=True)
        _, s, free = random.choice(options[:3])
        per_share, price = s["per_share"], s["price"]  # USDG (1e6): prefund and value of one share
        shares = min(held[sym] // 10**18, free // price, int(budget / (per_share * HEADROOM)))
        if shares < 1:
            no_demand.append(f"{sym}(budget)")
            continue
        amount = shares * 10**18
        prefund = int(core.functions.requiredPrefund(s["id"], amount).call() * HEADROOM) + 1
        try:
            fn = core.functions.depositShield(s["id"], amount, prefund)
            nonce = approve_if_needed(w3, acct, stock[sym], amount, nonce, tx_delay)
            nonce = approve_if_needed(w3, acct, usdg, prefund, nonce, tx_delay)
            try:
                fn.estimate_gas({"from": acct.address})  # simulate before paying gas
            except Exception as e:
                log(f"#{line_no} {acct.address}: #{s['id']} {sym} shield refused by contract: {str(e)[:150]}")
                continue
            h = send(w3, acct, fn, nonce)
            nonce += 1
            s["S_stock"] += amount  # keep the shared cache honest for the other threads
            made.append(f"#{s['id']} {sym} {shares} sh (~{shares * price / 1e6:,.0f} USDG) + {prefund / 1e6:,.2f} prefund")
            with FILE_LOCK, SHIELDS.open("a", encoding="utf-8") as f:
                f.write(f"{acct.address},{s['id']},{shares},{prefund / 1e6},{h}\n")
        except Exception as e:
            log(f"#{line_no} {acct.address}: #{s['id']} {sym} shield ERROR {str(e)[:200]}")
            nonce = w3.eth.get_transaction_count(acct.address, "pending")
        time.sleep(random.uniform(*tx_delay))

    left = usdg.functions.balanceOf(acct.address).call() / 1e6
    log(f"#{line_no} {acct.address}: shield [{', '.join(made) or '-'}], USDG left {left:,.2f}"
        + (f" | no free COUPON demand: {', '.join(no_demand)}" if no_demand else ""))
    return bool(made)


# ---------------------------------------------------------------- self-matched pairs
PAIRS = 3  # series per run for COUPON+SHIELD pairs (different stocks)


def imbalance(s):
    """How lopsided a series is, 0 = balanced. A pair in a balanced series is matched ~100%."""
    shield_value = s["S_stock"] * s["price"] // 10**18
    return abs(shield_value - s["D"]) / max(shield_value + s["D"], 1)


def pair_all(w3, line_no, acct, tx_delay):
    """Put the idle USDG to work: in up to PAIRS balanced series, a COUPON deposit of X USDG plus
    a SHIELD of the same value X from this account's own stock. Both positions earn points and
    they match each other. 90% of the USDG balance is used, 10% kept.
    Returns True when at least one pair was made."""
    core = w3.eth.contract(address=CORE, abi=CORE_ABI + [
        {"type": "function", "name": "depositCoupon", "stateMutability": "nonpayable",
         "inputs": [{"type": "uint256"}, {"type": "uint256"}], "outputs": []}])
    usdg = w3.eth.contract(address=TOKENS["USDG"], abi=ERC20_ABI + ERC20_EXTRA)
    series = open_series(w3)
    stock = {sym: w3.eth.contract(address=TOKENS[sym], abi=ERC20_ABI + ERC20_EXTRA)
             for sym in {s["symbol"] for s in series} if sym in TOKENS}
    held = {sym: c.functions.balanceOf(acct.address).call() for sym, c in stock.items()}
    cash = usdg.functions.balanceOf(acct.address).call() * 9 // 10

    # the 10 most balanced series in random order: good matching, but wallets spread out (anti-sybil)
    ranked = sorted(series, key=imbalance)
    top = ranked[:10]
    random.shuffle(top)
    chosen, seen = [], set()
    for s in top + ranked[10:]:
        if s["symbol"] not in seen and held.get(s["symbol"], 0) >= 10**18:
            chosen.append(s)
            seen.add(s["symbol"])
        if len(chosen) == PAIRS:
            break
    if not chosen or cash < 10**6:
        log(f"#{line_no} {acct.address}: pair skip, no idle USDG or stock")
        return False

    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    made, budget = [], cash // len(chosen)
    for s in chosen:
        sym, price, per_share = s["symbol"], s["price"], s["per_share"]
        # budget = X (COUPON) + prefund for X of SHIELD; one share costs price + per_share
        shares = min(held[sym] // 10**18, int(budget / (price + per_share * HEADROOM)))
        coupon = shares * price // 10_000 * 10_000
        if shares < 1 or coupon < s["min"]:
            continue
        amount = shares * 10**18
        prefund = int(core.functions.requiredPrefund(s["id"], amount).call() * HEADROOM) + 1
        try:
            nonce = approve_if_needed(w3, acct, usdg, coupon + prefund, nonce, tx_delay)
            hc = send(w3, acct, core.functions.depositCoupon(s["id"], coupon), nonce)
            nonce += 1
            s["D"] += coupon
            with FILE_LOCK, DEPOSITS.open("a", encoding="utf-8") as f:
                f.write(f"{acct.address},{s['id']},{coupon / 1e6},{hc}\n")
            time.sleep(random.uniform(*tx_delay))
            nonce = approve_if_needed(w3, acct, stock[sym], amount, nonce, tx_delay)
            nonce = approve_if_needed(w3, acct, usdg, prefund, nonce, tx_delay)
            h = send(w3, acct, core.functions.depositShield(s["id"], amount, prefund), nonce)
            nonce += 1
            s["S_stock"] += amount
            made.append(f"#{s['id']} {sym} {coupon / 1e6:,.0f} USDG + {shares} sh")
            with FILE_LOCK, SHIELDS.open("a", encoding="utf-8") as f:
                f.write(f"{acct.address},{s['id']},{shares},{prefund / 1e6},{h}\n")
        except Exception as e:
            log(f"#{line_no} {acct.address}: #{s['id']} {sym} pair ERROR {str(e)[:200]}")
            nonce = w3.eth.get_transaction_count(acct.address, "pending")
        time.sleep(random.uniform(*tx_delay))

    left = usdg.functions.balanceOf(acct.address).call() / 1e6
    log(f"#{line_no} {acct.address}: pairs [{', '.join(made) or '-'}], USDG left {left:,.2f}")
    return bool(made)
