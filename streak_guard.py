"""STREAK_GUARD: watches every wallet's note positions and saves the streak after a breach.

Per wallet (everything read from chain, no proxy needed):
- all positions: CouponDeposited / ShieldDeposited events of the wallet on NoteCore;
- health of every Live position, exactly like the app's Portfolio page:
  barrier = s0 * barrierBps / 1e4, buffer = (price - barrier) / price, where price is the
  latest feed print (if not older than 4 days) or the series' lastPrice.
  Breached < 0, At risk < 10%, Watch < 20%, Sound >= 20%. At risk / Breached are warned about;
- breaches: an Observed event with barrierHolds = false. The community Salvage Window gives
  48h from that observation to re-deposit the same size or bigger (50% of the streak kept,
  2x boost on the new note for a week). Within the window the guard re-enters a new open
  series of the same stock, same side, same size (rounded up). A breach counts as
  salvaged once the wallet has such a deposit after the breach (made by the guard, main.py or by hand).

Runs once per account from main.py, right after FAUCET, when config.STREAK_GUARD = 1.
"""
import math
import random
import threading
import time

from eth_abi import decode, encode
from web3 import Web3

import config
from deposit import (CORE, DEPLOY_BLOCK, DEPOSITS, ERC20_EXTRA, GET_SERIES, SERIES_VIEW, SYMBOLS, open_series, send,
                     shield_excess)
from faucet import ABI as ERC20_ABI, TOKENS
from register import FILE_LOCK, log
from shield import HEADROOM, SHIELDS, approve_if_needed

SALVAGE_WINDOW = 48 * 3600
FEED_MAX_AGE = 4 * 86400      # the app ignores feed prints older than this and falls back to lastPrice
LIVE = 2
STATUS = {0: "None", 1: "Subscription", 2: "Live", 3: "Autocalled", 4: "Matured", 5: "Cancelled", 6: "Unwound"}
LABEL = {"sound": "Sound", "watch": "Watch", "atRisk": "At risk", "breached": "Breached"}

T_COUPON = "0x" + Web3.keccak(text="CouponDeposited(uint256,address,uint256)").hex().removeprefix("0x")
T_SHIELD = "0x" + Web3.keccak(text="ShieldDeposited(uint256,address,uint256,uint256)").hex().removeprefix("0x")
T_OBSERVED = "0x" + Web3.keccak(text="Observed(uint256,uint32,uint256,bool,bool,uint8)").hex().removeprefix("0x")

FEED_ABI = [{"type": "function", "name": "latestRoundData", "stateMutability": "view", "inputs": [],
             "outputs": [{"type": "uint80"}, {"type": "int256"}, {"type": "uint256"}, {"type": "uint256"},
                         {"type": "uint80"}]}]
CORE_ABI = [
    {"type": "function", "name": "depositCoupon", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}], "outputs": []},
    {"type": "function", "name": "depositShield", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"}], "outputs": []},
    {"type": "function", "name": "requiredPrefund", "stateMutability": "view",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}]},
]

_lock = threading.Lock()
_block_ts = {}                      # block number -> timestamp (never changes)
_views = {"at": 0, "data": {}}      # series id -> SeriesView, refreshed every minute
_obs = {"at": 0, "data": {}}        # series id -> [breach observations], refreshed every minute
_feeds = {}                         # feed -> (at, answer, updatedAt)


# ---------------------------------------------------------------- chain reads (shared caches)
def get_logs(w3, topics, start=DEPLOY_BLOCK, end=None):
    """NoteCore logs; a range over the RPC's 10,000-log limit is split in halves."""
    end = w3.eth.block_number if end is None else end
    try:
        return w3.eth.get_logs({"address": CORE, "fromBlock": start, "toBlock": end, "topics": topics})
    except Exception as e:
        if "exceeds limit" not in str(e) or end <= start:
            raise
    mid = (start + end) // 2
    return get_logs(w3, topics, start, mid) + get_logs(w3, topics, mid + 1, end)


def block_ts(w3, number):
    with _lock:
        if number in _block_ts:
            return _block_ts[number]
    ts = w3.eth.get_block(number)["timestamp"]
    with _lock:
        _block_ts[number] = ts
    return ts


def series_view(w3, sid):
    with _lock:
        if time.time() - _views["at"] > 60:
            _views.update(at=time.time(), data={})
        if sid in _views["data"]:
            return _views["data"][sid]
    v = decode([SERIES_VIEW], w3.eth.call({"to": CORE, "data": GET_SERIES + encode(["uint256"], [sid])}))[0]
    with _lock:
        _views["data"][sid] = v
    return v


def breaches(w3):
    """series id -> [(observation index, price, block ts)] of observations below the barrier."""
    with _lock:
        if time.time() - _obs["at"] < 60:
            return _obs["data"]
        found = {}
        for lg in get_logs(w3, [T_OBSERVED]):
            price, holds, _autocall, _status = decode(["uint256", "bool", "bool", "uint8"], bytes(lg["data"]))
            if not holds:
                sid, idx = int(lg["topics"][1].hex(), 16), int(lg["topics"][2].hex(), 16)
                found.setdefault(sid, []).append((idx, price, lg["blockNumber"]))
    found = {sid: [(i, p, block_ts(w3, b)) for i, p, b in rows] for sid, rows in found.items()}
    with _lock:
        _obs.update(at=time.time(), data=found)
    return found


def feed_price(w3, feed, fallback):
    """Latest feed print (1e8) like the app: fresh feed answer, else the series' lastPrice."""
    with _lock:
        cached = _feeds.get(feed)
    if not cached or time.time() - cached[0] > 60:
        try:
            _, answer, _, updated, _ = w3.eth.contract(address=Web3.to_checksum_address(feed), abi=FEED_ABI) \
                .functions.latestRoundData().call()
        except Exception:
            answer, updated = 0, 0
        cached = (time.time(), answer, updated)
        with _lock:
            _feeds[feed] = cached
    _, answer, updated = cached
    now = time.time()
    if answer > 0 and 0 < updated <= now + 300 and now - updated <= FEED_MAX_AGE:
        return answer
    return fallback if fallback > 0 else 0


def wallet_deposits(w3, address):
    """All deposits of the wallet: [{sid, side, quote, stock, block}] (quote 1e6, stock 1e18)."""
    who = "0x" + "0" * 24 + address[2:].lower()
    rows = []
    for topic, side in ((T_COUPON, "coupon"), (T_SHIELD, "shield")):
        for lg in get_logs(w3, [topic, None, who]):
            sid = int(lg["topics"][1].hex(), 16)
            if side == "coupon":
                quote, stock = decode(["uint256"], bytes(lg["data"]))[0], 0
            else:
                stock, quote = decode(["uint256", "uint256"], bytes(lg["data"]))
            rows.append({"sid": sid, "side": side, "quote": quote, "stock": stock, "block": lg["blockNumber"]})
    return rows


def health(w3, v):
    """(class, buffer bps, price, barrier) of a Live series, same rule as the app's Portfolio."""
    s0, barrier_bps, last = v[17], v[3], v[18]
    if v[13] != LIVE or s0 <= 0:
        return None
    barrier = s0 * barrier_bps // 10_000
    price = feed_price(w3, v[1], last)
    if price <= 0:
        return None
    buffer = (price - barrier) * 10_000 // price
    cls = "breached" if price < barrier else "atRisk" if buffer < 1000 else "watch" if buffer < 2000 else "sound"
    return cls, buffer, price, barrier


# ---------------------------------------------------------------- re-entry
def rounded_up(amount, step):
    """The breached size rounded up to a whole `step`: the same size or a little bigger."""
    return math.ceil(amount / step) * step


def candidates(w3, symbol, barrier_bps, exclude):
    """Open series of the same stock: the same barrier tier first, then the most SHIELD excess."""
    same = [s for s in open_series(w3) if s["symbol"] == symbol and s["id"] not in exclude]
    same.sort(key=lambda s: (series_view(w3, s["id"])[3] == barrier_bps, shield_excess(s)), reverse=True)
    return same


def reenter_coupon(w3, line_no, acct, symbol, barrier_bps, size, exclude):
    usdg = w3.eth.contract(address=TOKENS["USDG"], abi=ERC20_ABI + ERC20_EXTRA)
    core = w3.eth.contract(address=CORE, abi=CORE_ABI)
    balance = usdg.functions.balanceOf(acct.address).call()
    if balance < size:
        return None, f"not enough USDG: need {size / 1e6:,.2f}, have {balance / 1e6:,.2f}"
    for s in candidates(w3, symbol, barrier_bps, exclude):
        amount = min(max(rounded_up(size, 10**6), s["min"]), balance)
        if amount < size or amount < s["min"] or amount > s["room"]:
            continue
        fn = core.functions.depositCoupon(s["id"], amount)
        nonce = w3.eth.get_transaction_count(acct.address, "pending")
        nonce = approve_if_needed(w3, acct, usdg, amount, nonce, config.DELAY_BETWEEN_TX)
        try:
            fn.estimate_gas({"from": acct.address})  # simulate: the cached series may have closed
        except Exception as e:
            log(f"#{line_no} {acct.address}: streak guard #{s['id']} refused: {str(e)[:120]}")
            continue
        h = send(w3, acct, fn, nonce)
        s["D"] += amount
        with FILE_LOCK, DEPOSITS.open("a", encoding="utf-8") as f:
            f.write(f"{acct.address},{s['id']},{amount / 1e6},{h}\n")
        return f"#{s['id']} {symbol} COUPON {amount / 1e6:,.2f} USDG", None
    return None, f"no open {symbol} series takes {size / 1e6:,.2f} USDG"


def reenter_shield(w3, line_no, acct, symbol, barrier_bps, size, exclude):
    usdg = w3.eth.contract(address=TOKENS["USDG"], abi=ERC20_ABI + ERC20_EXTRA)
    stock = w3.eth.contract(address=TOKENS[symbol], abi=ERC20_ABI + ERC20_EXTRA)
    core = w3.eth.contract(address=CORE, abi=CORE_ABI)
    held = stock.functions.balanceOf(acct.address).call()
    if held < size:
        return None, f"not enough {symbol}: need {size / 1e18:g}, have {held / 1e18:g}"
    amount = min(rounded_up(size, 10**18), held)
    for s in candidates(w3, symbol, barrier_bps, exclude):
        prefund = int(core.functions.requiredPrefund(s["id"], amount).call() * HEADROOM) + 1
        if usdg.functions.balanceOf(acct.address).call() < prefund:
            return None, f"not enough USDG for the {prefund / 1e6:,.2f} prefund"
        fn = core.functions.depositShield(s["id"], amount, prefund)
        nonce = w3.eth.get_transaction_count(acct.address, "pending")
        nonce = approve_if_needed(w3, acct, stock, amount, nonce, config.DELAY_BETWEEN_TX)
        nonce = approve_if_needed(w3, acct, usdg, prefund, nonce, config.DELAY_BETWEEN_TX)
        try:
            fn.estimate_gas({"from": acct.address})
        except Exception as e:
            log(f"#{line_no} {acct.address}: streak guard #{s['id']} refused: {str(e)[:120]}")
            continue
        h = send(w3, acct, fn, nonce)
        s["S_stock"] += amount
        with FILE_LOCK, SHIELDS.open("a", encoding="utf-8") as f:
            f.write(f"{acct.address},{s['id']},{amount / 1e18:g},{prefund / 1e6},{h}\n")
        return f"#{s['id']} {symbol} SHIELD {amount / 1e18:g} sh + {prefund / 1e6:,.2f} prefund", None
    return None, f"no open {symbol} series for SHIELD"


# ---------------------------------------------------------------- per wallet
def guard(w3, line_no, acct):
    """Health report + salvage re-entry for one wallet. Returns True when a tx was sent."""
    now = time.time()
    rows = wallet_deposits(w3, acct.address)
    if not rows:
        log(f"#{line_no} {acct.address}: streak guard, no positions")
        return False
    for r in rows:
        r["ts"] = block_ts(w3, r["block"])

    # positions = (series, side) with the wallet's total size in it
    positions = {}
    for r in rows:
        p = positions.setdefault((r["sid"], r["side"]), {"quote": 0, "stock": 0})
        p["quote"] += r["quote"]
        p["stock"] += r["stock"]
    views = {sid: series_view(w3, sid) for sid in {sid for sid, _ in positions}}
    symbol = {sid: SYMBOLS.get(v[0].lower(), v[0][:8]) for sid, v in views.items()}

    counts, warnings = {k: 0 for k in LABEL}, []
    other = {}
    for (sid, side), p in sorted(positions.items()):
        v = views[sid]
        h = health(w3, v)
        if h is None:
            other[STATUS.get(v[13], v[13])] = other.get(STATUS.get(v[13], v[13]), 0) + 1
            continue
        cls, buffer, price, barrier = h
        counts[cls] += 1
        if cls in ("atRisk", "breached"):
            warnings.append(f"!! {LABEL[cls].upper()} #{sid} {symbol[sid]} {side.upper()}: price {price / 1e8:,.2f}, "
                            f"barrier {barrier / 1e8:,.2f} ({buffer / 100:+.1f}% to barrier), "
                            f"obs {v[15] - 1}/{len(v[12]) - 1} done")
    live = sum(counts.values())
    summary = ", ".join(f"{LABEL[k]} {n}" for k, n in counts.items() if n) or "-"
    rest = ", ".join(f"{k} {n}" for k, n in other.items())
    log(f"#{line_no} {acct.address}: streak guard | live {live}: {summary}" + (f" | {rest}" if rest else ""))
    for w in warnings:
        log(f"#{line_no} {acct.address}: {w}")

    # breaches in the wallet's series -> salvage within 48h
    acted = False
    obs = breaches(w3)
    for (sid, side), p in sorted(positions.items()):
        for idx, price, ts in obs.get(sid, []):
            left = ts + SALVAGE_WINDOW - now
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))
            tag = f"#{sid} {symbol[sid]} {side.upper()} breached at obs {idx} ({when}, {price / 1e8:,.2f})"
            size = p["quote"] if side == "coupon" else p["stock"]
            key = "quote" if side == "coupon" else "stock"
            done = [r for r in rows if r["side"] == side and r["sid"] != sid and r["ts"] >= ts
                    and symbol.get(r["sid"]) == symbol[sid] and r[key] >= size]
            if done:
                log(f"#{line_no} {acct.address}: {tag} -> salvaged by #{done[0]['sid']}")
                continue
            if left <= 0:
                log(f"#{line_no} {acct.address}: {tag} -> salvage window closed")
                continue
            log(f"#{line_no} {acct.address}: !! {tag} -> {left / 3600:.1f}h left to re-enter, re-entering")
            fn = reenter_coupon if side == "coupon" else reenter_shield
            try:
                made, why = fn(w3, line_no, acct, symbol[sid], views[sid][3], size, {sid})
            except Exception as e:
                made, why = None, f"ERROR {str(e)[:200]}"
            if made:
                acted = True
                log(f"#{line_no} {acct.address}: streak saved, re-entered {made}")
                time.sleep(random.uniform(*config.DELAY_BETWEEN_TX))
            else:
                log(f"#{line_no} {acct.address}: !! re-entry for #{sid} FAILED: {why} ({left / 3600:.1f}h left)")
    return acted
