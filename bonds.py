"""BONDS: claim vested NOTE and buy a small USDG bond. Used by main.py.

Per account on every run:
1. claim: redeem every bond position that has vested NOTE (pendingPayout > 0)
2. buy: one small bond for BOND_USDG (random in range) on an open USDG market, with the
   same guards as the app (maxPrice = quoted price + 1%, minPayout = quote - 1%, 10 min deadline).
Pre-season: the Bonds stream (8% of the weekly pool) pays NOTE payout x vesting days, credited at
purchase, and the first bond completes the "bond" quest. The vested NOTE (24h) feeds apps.py:
staking, locks, gauge votes and the buyback.
"""
import random
import threading
import time
from pathlib import Path

from deposit import ERC20_EXTRA, send
from faucet import ABI as ERC20_ABI, TOKENS
from register import log

BASE = Path(__file__).resolve().parent
BOND_DEPOSITORY = "0x65d962DEef22f3ea2f51379C70709202fAcD63E1"  # final testnet build
ERC20 = 0  # BondDepository.QuoteKind

BOND_ABI = [
    {"type": "function", "name": "marketCount", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "market", "stateMutability": "view", "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "tuple", "components": [
         {"name": "kind", "type": "uint8"}, {"name": "active", "type": "bool"},
         {"name": "quoteToken", "type": "address"}, {"name": "legSeriesId", "type": "uint256"},
         {"name": "capacity", "type": "uint256"}, {"name": "totalDebt", "type": "uint256"},
         {"name": "controlVariable", "type": "uint256"}, {"name": "minPriceWad", "type": "uint256"},
         {"name": "maxDebt", "type": "uint256"}, {"name": "vestingSeconds", "type": "uint48"},
         {"name": "conclusion", "type": "uint48"}, {"name": "lastDecay", "type": "uint48"},
         {"name": "sold", "type": "uint256"}, {"name": "purchased", "type": "uint256"}]}]},
    {"type": "function", "name": "marketPrice", "stateMutability": "view", "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "payoutFor", "stateMutability": "view",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "bondCount", "stateMutability": "view", "inputs": [{"type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "pendingPayout", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "redeem", "stateMutability": "nonpayable",
     "inputs": [{"name": "indexes", "type": "uint256[]"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "depositWithLimits", "stateMutability": "nonpayable",
     "inputs": [{"name": "id", "type": "uint256"}, {"name": "amount", "type": "uint256"},
                {"name": "maxPriceWad", "type": "uint256"}, {"name": "minPayout", "type": "uint256"},
                {"name": "deadline", "type": "uint256"}, {"name": "recipient", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
]

_markets = {"at": 0, "ids": []}
_lock = threading.Lock()


def usdg_markets(w3, bd):
    """Open USDG bond markets with capacity left. Shared by all threads, refreshed every 10 min."""
    with _lock:
        if time.time() - _markets["at"] < 600:
            return _markets["ids"]
        ids, now = [], time.time()
        for i in range(bd.functions.marketCount().call()):
            m = bd.functions.market(i).call()
            if (m[0] == ERC20 and m[1] and m[2].lower() == TOKENS["USDG"].lower()
                    and m[4] > 0 and m[10] > now):
                ids.append(i)
        _markets.update(at=time.time(), ids=ids)
        return ids


def bonds_all(w3, line_no, acct, usdg_range, tx_delay):
    """Claim vested NOTE, then buy one small bond. Returns True when something was sent."""
    bd = w3.eth.contract(address=BOND_DEPOSITORY, abi=BOND_ABI)
    usdg = w3.eth.contract(address=TOKENS["USDG"], abi=ERC20_ABI + ERC20_EXTRA)
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    parts = []

    # 1. claim everything that has vested
    pending = [(i, bd.functions.pendingPayout(acct.address, i).call())
               for i in range(bd.functions.bondCount(acct.address).call())]
    ready = [i for i, p in pending if p > 0]
    if ready:
        try:
            send(w3, acct, bd.functions.redeem(ready), nonce)
            nonce += 1
            parts.append(f"claimed {sum(p for _, p in pending) / 1e18:,.2f} NOTE from {len(ready)} bond(s)")
            time.sleep(random.uniform(*tx_delay))
        except Exception as e:
            log(f"#{line_no} {acct.address}: bond claim ERROR {str(e)[:150]}")
            nonce = w3.eth.get_transaction_count(acct.address, "pending")

    # 2. buy one small bond
    ids = usdg_markets(w3, bd)
    amount = int(random.uniform(*usdg_range) * 100) * 10_000  # USDG, 2 decimals
    if not ids:
        parts.append("buy skip: no open USDG market")
    elif usdg.functions.balanceOf(acct.address).call() < amount:
        parts.append("buy skip: not enough USDG")
    else:
        mid = random.choice(ids)
        try:
            price = bd.functions.marketPrice(mid).call()
            payout = bd.functions.payoutFor(mid, amount).call()
            m = bd.functions.market(mid).call()
            room = m[8] - m[5]  # maxDebt - totalDebt: over it the contract reverts ExceedsMaxDebt
            if payout > room > 0:  # nearly full market: buy what still fits (90% of the room, price moves)
                amount = amount * room * 9 // 10 // payout // 10_000 * 10_000
                payout = bd.functions.payoutFor(mid, amount).call() if amount >= 10**6 else 0
            if not payout or m[5] + payout > m[8]:
                parts.append(f"buy skip: market {mid} full (debt {m[5] / 1e18:,.0f} / {m[8] / 1e18:,.0f} NOTE)")
            else:
                if usdg.functions.allowance(acct.address, BOND_DEPOSITORY).call() < amount:
                    send(w3, acct, usdg.functions.approve(BOND_DEPOSITORY, amount), nonce)
                    nonce += 1
                    time.sleep(random.uniform(*tx_delay))
                send(w3, acct, bd.functions.depositWithLimits(
                    mid, amount, price * 101 // 100, payout * 99 // 100, int(time.time()) + 600, acct.address), nonce)
                parts.append(f"bought {amount / 1e6:g} USDG -> ~{payout / 1e18:,.2f} NOTE (market {mid})")
        except Exception as e:
            log(f"#{line_no} {acct.address}: bond buy ERROR {str(e)[:150]}")

    log(f"#{line_no} {acct.address}: bonds [{'; '.join(parts) or '-'}]")
    return bool(ready) or any(p.startswith("bought") for p in parts)
