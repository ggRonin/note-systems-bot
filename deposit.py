"""COUPON deposits into open Note Systems series. Used by main.py.

Per account: DEPOSIT_COUNT deposits into random open series (different stocks
first), each DEPOSIT_PCT % of the account's USDG balance. Every deposit is an
on-chain approve + depositCoupon on NoteCore, sent directly to the Robinhood
testnet RPC. Every run makes DEPOSIT_COUNT new deposits; deposits_v2.txt records
them so the account never deposits twice into the same open series.
"""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eth_abi import decode, encode
from web3 import Web3

from faucet import ABI as ERC20_ABI, CHAIN_ID, TOKENS
from register import FILE_LOCK, log, read_lines

BASE = Path(__file__).resolve().parent
DEPOSITS = BASE / "deposits_v2.txt"   # deposits.txt is the Season 0 core: its series ids mean other series

CORE = "0x55eA7977419A3848ac2E899f7e9504b00f5d28F7"  # final testnet build (pre-season)
DEPLOY_BLOCK = 127302759
SUBSCRIPTION = 1          # INoteCore.Status
CLOSE_MARGIN = 30 * 60    # skip series whose subscription ends within 30 min
SYMBOLS = {a.lower(): s for s, a in TOKENS.items() if s != "USDG"}

CORE_ABI = [
    {"type": "function", "name": "seriesCount", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "seriesStatus", "stateMutability": "view",
     "inputs": [{"type": "uint256"}], "outputs": [{"type": "uint8"}]},
    {"type": "function", "name": "depositCoupon", "stateMutability": "nonpayable",
     "inputs": [{"name": "seriesId", "type": "uint256"}, {"name": "quoteAmount", "type": "uint256"}],
     "outputs": []},
    {"type": "function", "name": "requiredPrefund", "stateMutability": "view",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}]},
]
ERC20_EXTRA = [
    {"type": "function", "name": "allowance", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
]
# INoteCore.SeriesView, decoded by hand (getSeries returns a struct with a dynamic array)
SERIES_VIEW = ("(address,address,uint16,uint16,uint16,uint16,uint16,uint16,uint16,uint40,uint128,uint128,"
               "uint40[],uint8,uint16,uint32,bool,uint256,uint256,uint256,uint256,uint256,uint256,uint256,"
               "uint256,uint256)")
GET_SERIES = Web3.keccak(text="getSeries(uint256)")[:4]

_cache = {"at": 0, "series": []}
_cache_lock = threading.Lock()


def open_series(w3):
    """Series that accept COUPON deposits now. Shared by all threads, refreshed every 10 min."""
    with _cache_lock:
        if time.time() - _cache["at"] < 600:
            return _cache["series"]
        core = w3.eth.contract(address=CORE, abi=CORE_ABI)

        def get(sid):
            try:
                raw = w3.eth.call({"to": CORE, "data": GET_SERIES + encode(["uint256"], [sid])})
                return sid, decode([SERIES_VIEW], raw)[0]
            except Exception:  # unknown id reverts
                return sid, None

        # One getSeries per id (it carries the status too), 16 in parallel: the RPC takes ~1s per call.
        # Ids are 0-based: seriesCount() == last id + 1.
        with ThreadPoolExecutor(max_workers=16) as pool:
            views = list(pool.map(get, range(core.functions.seriesCount().call())))
        now, found = time.time(), []
        for sid, v in views:
            if v is None:
                continue
            status, sub_end, cap, min_ticket, deposited = v[13], v[9], v[10], v[11], v[19]
            if status != SUBSCRIPTION or sub_end - now < CLOSE_MARGIN:
                continue
            found.append({"id": sid, "symbol": SYMBOLS.get(v[0].lower(), v[0][:8]), "min": min_ticket,
                          "room": max(cap - deposited, 0), "sub_end": sub_end,
                          # for SHIELD sizing: COUPON demand D, SHIELD stock posted, prefund rate
                          "D": deposited, "S_stock": v[20],
                          "prefund_bps": v[5] * (len(v[12]) - 1) + v[8]})

        def price(s):
            # requiredPrefund of one share / prefund rate = the reference price in USDG (1e6) per share
            s["per_share"] = core.functions.requiredPrefund(s["id"], 10**18).call()
            s["price"] = s["per_share"] * 10**4 // s["prefund_bps"]

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(price, found))
        _cache.update(at=time.time(), series=found)
        log(f"open series: {len(found)} ({', '.join(str(s['id']) for s in found)})")
        return found


def shield_excess(s):
    """SHIELD value posted beyond COUPON demand (USDG, 1e6): what a COUPON deposit fully matches."""
    return s["S_stock"] * s["price"] // 10**18 - s["D"]


def coupon_free(s):
    """COUPON demand not yet covered by SHIELD (USDG, 1e6): what a SHIELD deposit fully matches."""
    return -shield_excess(s)


def active_deposits(address, open_ids):
    """Series ids from deposits_v2.txt where this address deposited and that are still open."""
    ids = set()
    for line in read_lines(DEPOSITS):
        addr, sid = line.split(",")[:2]
        if addr == address and int(sid) in open_ids:
            ids.add(int(sid))
    return ids


def pick(series, taken, count):
    """Series with the biggest SHIELD excess first (a COUPON deposit there is fully matched and
    the discovered coupon is higher), one per stock first. If there are not enough, the rest are
    random series: the SHIELD step that runs next then matches them with this account's stock."""
    pool = [s for s in series if s["id"] not in taken]
    matched = sorted([s for s in pool if shield_excess(s) >= s["min"]], key=shield_excess, reverse=True)
    # random order among the best candidates, so wallets don't all pick the same series (anti-sybil)
    top = matched[:max(count * 2, 8)]
    random.shuffle(top)
    matched = top + matched[len(top):]
    rest = [s for s in pool if s not in matched]
    random.shuffle(rest)
    chosen, stocks = [], set()
    for group in (matched, rest):
        for s in group:
            if s["symbol"] not in stocks:
                chosen.append(s)
                stocks.add(s["symbol"])
    chosen += [s for s in matched + rest if s not in chosen]
    return chosen[:count]


_next_nonce = {}  # address -> next nonce after our last sent tx (callers' own counters can go stale)
NONCE_ERRORS = ("nonce too low", "already known", "replacement transaction underpriced")


def is_nonce_error(e):
    return any(m in str(e).lower() for m in NONCE_ERRORS)


def sent_already(w3, tx_hash):
    try:
        w3.eth.get_transaction(tx_hash)
        return True
    except Exception:
        return False


def send(w3, acct, fn, nonce, value=0):
    """Sign, send and wait. When the wallet's nonce was taken by another sender (e.g. privacy pool
    relays from the same wallet), the tx is re-sent with a fresh nonce instead of failing."""
    nonce = max(nonce, _next_nonce.get(acct.address, 0))
    for attempt in range(5):
        tx = fn.build_transaction({"chainId": CHAIN_ID, "from": acct.address, "nonce": nonce, "value": value,
                                   "maxFeePerGas": w3.eth.gas_price * 2, "maxPriorityFeePerGas": 0})
        tx["gas"] = int(tx["gas"] * 1.3)  # Arbitrum Orbit: estimate covers L1 data too, add headroom
        signed = acct.sign_transaction(tx)
        try:
            h = w3.eth.send_raw_transaction(signed.raw_transaction)
            break
        except Exception as e:
            if not is_nonce_error(e) or attempt == 4:
                raise
            if sent_already(w3, signed.hash):  # rpc.py re-sent our own tx after a timeout: it went through
                h = signed.hash
                break
            time.sleep(1 + attempt)
            nonce = max(nonce + 1, w3.eth.get_transaction_count(acct.address, "pending"))
    _next_nonce[acct.address] = nonce + 1
    if w3.eth.wait_for_transaction_receipt(h, timeout=180).status != 1:
        raise RuntimeError(f"tx reverted {h.hex()}")
    return h.hex() if h.hex().startswith("0x") else "0x" + h.hex()


def deposit_all(w3, line_no, acct, count, pct, tx_delay):
    """Make up to `count` COUPON deposits. Returns True when at least one was made."""
    usdg = w3.eth.contract(address=TOKENS["USDG"], abi=ERC20_ABI + ERC20_EXTRA)
    core = w3.eth.contract(address=CORE, abi=CORE_ABI)
    series = open_series(w3)
    taken = active_deposits(acct.address, {s["id"] for s in series})  # already used: pick other series
    need = count

    balance = usdg.functions.balanceOf(acct.address).call()
    if balance == 0:
        log(f"#{line_no} {acct.address}: deposit skip, no USDG (run the faucet)")
        return False
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    made = []
    for s in pick(series, taken, need):
        # 5-15% of the balance at the start, human-looking rounding (whole or 2 decimals)
        amount = balance * random.uniform(*pct) / 100
        amount = int(amount // 1_000_000 * 1_000_000) if random.random() < 0.6 else int(amount // 10_000 * 10_000)
        excess = shield_excess(s)
        if excess >= s["min"]:
            amount = min(amount, excess // 10_000 * 10_000)  # no more than the SHIELD side matches
        amount = max(amount, s["min"])
        left = usdg.functions.balanceOf(acct.address).call()
        if amount > left or amount > s["room"]:
            log(f"#{line_no} {acct.address}: #{s['id']} {s['symbol']} skip, amount {amount / 1e6} over balance/cap")
            continue
        try:
            if usdg.functions.allowance(acct.address, CORE).call() < amount:
                send(w3, acct, usdg.functions.approve(CORE, amount), nonce)
                nonce += 1
                time.sleep(random.uniform(*tx_delay))
            h = send(w3, acct, core.functions.depositCoupon(s["id"], amount), nonce)
            nonce += 1
            s["D"] += amount  # keep the shared cache honest for the other threads and the SHIELD step
            made.append(f"#{s['id']} {s['symbol']} {amount / 1e6:g}" + ("" if excess >= s["min"] else " (unmatched yet)"))
            with FILE_LOCK, DEPOSITS.open("a", encoding="utf-8") as f:
                f.write(f"{acct.address},{s['id']},{amount / 1e6},{h}\n")
        except Exception as e:
            log(f"#{line_no} {acct.address}: #{s['id']} {s['symbol']} deposit ERROR {str(e)[:200]}")
            nonce = w3.eth.get_transaction_count(acct.address, "pending")
        time.sleep(random.uniform(*tx_delay))

    left = usdg.functions.balanceOf(acct.address).call() / 1e6
    log(f"#{line_no} {acct.address}: deposited [{', '.join(made) or '-'}], USDG left {left:,.2f}")
    return bool(made)
