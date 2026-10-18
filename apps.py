"""Pre-season apps beyond notes and bonds: Desk, Staking, Locks, Gauge votes, Buyback, Omnichain, Vaults.

The pre-season pays a weekly pool split by stream, and the breadth multiplier grows with every
app a wallet uses in the week (1 + 0.25 per extra app, capped at 2x). Each function here is one
app, run from main.py when its config flag is on. All of them only add (deposit, stake, lock,
vote, sell, move) and never withdraw: a Desk withdrawal request inside 7 days of a deposit or an
sNOTE cooldown request forfeits that stream's week.

NOTE comes from the USDG bond that bonds.py buys every run (24h vesting, claimed on the next run).
Every call is simulated (estimate_gas) before it is sent, so a refused step costs no gas.
"""
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from web3 import Web3

from deposit import CORE, send
from faucet import TOKENS
from register import FILE_LOCK, log, read_lines

BASE = Path(__file__).resolve().parent
ABI = json.loads((BASE / "abi_v2.json").read_text(encoding="utf-8"))

NOTE = "0x8683C8684e989D93eA8C96B9331E7Eff2F21276E"
SNOTE = "0x43dADb9E8DE0a4D244973810B90017388dD8FD96"
DESK = "0x0CABd56Cd7cC549d3dAc12d18d555739324015D8"
VENOTE = "0xDA29ee0Db7Ecd439E45387722A28C122F944f545"
GAUGE_CONTROLLER = "0x118882729977f459518822038ab6716BfA0aAB9b"
BUYBACK = "0xEAbAC946A642cE1Eb9933e78b0CFEd41af304240"  # RevenueRouter: sellNoteForQuote
NOTE_LEGS = "0x5e0a3C515142a226abefFD00975020e89f30d1c4"
LEG_VAULT = "0x64a3Ce8b9fb824c3e3532439B5A1dD27A23d5f4A"
ARB_SEPOLIA_EID = 40231  # the only leg destination (LayerZero endpoint id)
LEGS_SENT = BASE / "legs_v2.txt"  # "address,legId,units,tx": one move per leg, so the rest stays home
# NoteVaultFactory on the pre-season core. The SDK on the site (0.1.1) still lists the Season 0 factory 0x2BB2...,
# whose one vault takes the old USDG and is not counted.
VAULT_FACTORY = "0x9bAb91150CB9538B63768E9E892c9C0a7Cd36Cd8"
VAULTS_HELD = BASE / "vaults_v2.txt"  # "address,vault,usdg,tx": a wallet keeps topping up the vault it picked first
VAULT_MIN_ASSETS = 10 * 10**6  # a vault with less is someone's empty test deploy
LIVE = 2
WEEK = 7 * 86400

FACTORY_ABI = [
    {"type": "function", "name": "vaultCount", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "vaults", "stateMutability": "view",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}], "outputs": [{"type": "address[]"}]},
]
VAULT_ABI = [  # NoteVault (ERC-4626), the part used here
    {"type": "function", "name": "asset", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "name", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
    {"type": "function", "name": "totalAssets", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "maxDeposit", "stateMutability": "view", "inputs": [{"type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "previewDeposit", "stateMutability": "view", "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "deposit", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view", "inputs": [{"type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "convertToAssets", "stateMutability": "view", "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "uint256"}]},
]
_vaults = {"at": 0, "list": []}
_vaults_lock = threading.Lock()


def contract(w3, address, name):
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=ABI[name])


def simulate(fn, acct, value=0):
    """None when the call would pass, else the short revert reason."""
    try:
        fn.estimate_gas({"from": acct.address, "value": value})
        return None
    except Exception as e:
        return str(e)[:150]


def approve(w3, acct, token, spender, amount, nonce, tx_delay):
    if token.functions.allowance(acct.address, spender).call() >= amount:
        return nonce
    send(w3, acct, token.functions.approve(spender, amount), nonce)
    time.sleep(random.uniform(*tx_delay))
    return nonce + 1


def pct_of(balance, pct, step):
    """A random pct of the balance, rounded down to a whole `step` (looks typed by hand)."""
    return int(balance * random.uniform(*pct) / 100) // step * step


# ---------------------------------------------------------------- Desk (12% of the pool)
def desk(w3, line_no, acct, pct, tx_delay):
    """Deposit pct % of the USDG balance into the Desk (dUSDG held x days). Never withdraws."""
    usdg = contract(w3, TOKENS["USDG"], "erc20")
    vault = contract(w3, DESK, "desk")
    amount = pct_of(usdg.functions.balanceOf(acct.address).call(), pct, 10**6)
    room = vault.functions.maxDeposit(acct.address).call()  # the Desk caps deposits (0 = full for now)
    if room < 10**6:
        log(f"#{line_no} {acct.address}: desk skip, the Desk takes no deposits now (full)")
        return False
    amount = min(amount, room // 10**6 * 10**6)
    if amount < max(vault.functions.minRequestQuote().call(), 10**6):
        log(f"#{line_no} {acct.address}: desk skip, not enough USDG")
        return False
    if vault.functions.previewDeposit(amount).call() == 0:
        log(f"#{line_no} {acct.address}: desk skip, vault quotes zero shares (paused?)")
        return False
    nonce = approve(w3, acct, usdg, DESK, amount, w3.eth.get_transaction_count(acct.address, "pending"), tx_delay)
    fn = vault.functions.deposit(amount, acct.address)
    why = simulate(fn, acct)
    if why:
        log(f"#{line_no} {acct.address}: desk refused: {why}")
        return False
    send(w3, acct, fn, nonce)
    held = vault.functions.balanceOf(acct.address).call()
    log(f"#{line_no} {acct.address}: desk +{amount / 1e6:,.0f} USDG, dUSDG {held / 10 ** vault.functions.decimals().call():,.2f}")
    return True


# ---------------------------------------------------------------- Staking (6%)
def stake(w3, line_no, acct, pct, tx_delay):
    """Stake pct % of the NOTE balance into sNOTE (sNOTE held x days). Never starts a cooldown."""
    note = contract(w3, NOTE, "erc20")
    snote = contract(w3, SNOTE, "snote")
    amount = pct_of(note.functions.balanceOf(acct.address).call(), pct, 10**18)
    if amount < 10**18:
        log(f"#{line_no} {acct.address}: stake skip, no NOTE (bonds vest 24h)")
        return False
    if snote.functions.previewDeposit(amount).call() == 0:
        log(f"#{line_no} {acct.address}: stake skip, sNOTE quotes zero shares")
        return False
    nonce = approve(w3, acct, note, SNOTE, amount, w3.eth.get_transaction_count(acct.address, "pending"), tx_delay)
    fn = snote.functions.deposit(amount, acct.address)
    why = simulate(fn, acct)
    if why:
        log(f"#{line_no} {acct.address}: stake refused: {why}")
        return False
    send(w3, acct, fn, nonce)
    log(f"#{line_no} {acct.address}: staked {amount / 1e18:,.0f} NOTE")
    return True


# ---------------------------------------------------------------- Locks (7%)
def lock(w3, line_no, acct, pct, days, tx_delay):
    """Lock pct % of the NOTE balance in veNOTE (locked x days x lock multiplier 1-2.5, longer = more).
    No lock yet: createLock for `days`; a live lock: increaseAmount; an expired one: withdraw first."""
    note = contract(w3, NOTE, "erc20")
    ve = contract(w3, VENOTE, "venote")
    amount = pct_of(note.functions.balanceOf(acct.address).call(), pct, 10**18)
    if amount < 10**18:
        log(f"#{line_no} {acct.address}: lock skip, no NOTE")
        return False
    locked, end = ve.functions.locked(acct.address).call()[:2]
    now = int(time.time())
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    if locked > 0 and end <= now:  # expired: take it out, then lock everything again
        send(w3, acct, ve.functions.withdraw(), nonce)
        nonce += 1
        time.sleep(random.uniform(*tx_delay))
        locked = 0
    nonce = approve(w3, acct, note, VENOTE, amount, nonce, tx_delay)
    if locked > 0:
        fn, what = ve.functions.increaseAmount(amount), "added to lock"
    else:
        unlock = min(now + int(random.uniform(*days) * 86400), now + ve.functions.MAXTIME().call() - WEEK)
        fn, what = ve.functions.createLock(amount, unlock), f"locked until {time.strftime('%Y-%m-%d', time.gmtime(unlock))}"
    why = simulate(fn, acct)
    if why:
        log(f"#{line_no} {acct.address}: lock refused: {why}")
        return False
    send(w3, acct, fn, nonce)
    log(f"#{line_no} {acct.address}: {amount / 1e18:,.0f} NOTE {what}")
    return True


# ---------------------------------------------------------------- Gauge votes (2%)
def vote(w3, line_no, acct):
    """All voting power on one gauge. A gauge can be re-voted once WEIGHT_VOTE_DELAY (10 days) has
    passed, so the wallet votes again on its gauge whenever that is allowed."""
    ve = contract(w3, VENOTE, "venote")
    gc = contract(w3, GAUGE_CONTROLLER, "gaugecontroller")
    if ve.functions.balanceOf(acct.address).call() == 0:
        log(f"#{line_no} {acct.address}: vote skip, no veNOTE (lock NOTE first)")
        return False
    gauges = gc.functions.allGauges().call()
    delay = gc.functions.WEIGHT_VOTE_DELAY().call()
    last = {g: gc.functions.lastUserVote(acct.address, g).call() for g in gauges}
    mine = [g for g, ts in last.items() if ts]
    if gc.functions.voteUserPower(acct.address).call() and mine:
        g = max(mine, key=last.get)  # power already placed: renew the vote on the same gauge
    else:
        g = random.choice([g for g in gauges if not gc.functions.isKilled(g).call()])
    left = last[g] + delay - time.time()
    if left > 0:
        log(f"#{line_no} {acct.address}: vote skip, next vote in {left / 86400:.1f}d")
        return False
    fn = gc.functions.voteForGaugeWeights(g, 10_000)
    why = simulate(fn, acct)
    if why:
        log(f"#{line_no} {acct.address}: vote refused: {why}")
        return False
    send(w3, acct, fn, w3.eth.get_transaction_count(acct.address, "pending"))
    log(f"#{line_no} {acct.address}: voted 100% for gauge {g[:10]}")
    return True


# ---------------------------------------------------------------- Buyback (3%)
def buyback(w3, line_no, acct, tx_delay):
    """Sell a little NOTE into the buyback auction (USDG filled counts). The auction fills only
    while it holds a USDG reserve and the min interval since the last fill has passed."""
    note = contract(w3, NOTE, "erc20")
    bb = contract(w3, BUYBACK, "buyback")
    if bb.functions.paused().call():
        log(f"#{line_no} {acct.address}: buyback skip, paused")
        return False
    max_fill = bb.functions.maxFillQuote().call()
    min_fill = max(bb.functions.minFillQuote().call(), bb.functions.minFillQuoteAbs().call())
    if max_fill < min_fill:
        log(f"#{line_no} {acct.address}: buyback skip, auction has no USDG reserve")
        return False
    wait = bb.functions.lastFillTime().call() + bb.functions.minFillInterval().call() - time.time()
    if wait > 0:
        log(f"#{line_no} {acct.address}: buyback skip, next fill in {wait / 60:.0f} min")
        return False
    price = bb.functions.currentPriceWad().call()  # USDG (1e6) per NOTE, wad
    target = min(max_fill, min_fill * random.uniform(1.1, 1.5))
    amount = int(target * 10**18 // price * 10**12) // 10**18 * 10**18 + 10**18  # whole NOTE
    if note.functions.balanceOf(acct.address).call() < amount:
        log(f"#{line_no} {acct.address}: buyback skip, needs {amount / 1e18:,.0f} NOTE")
        return False
    out = bb.functions.previewSell(amount).call()
    nonce = approve(w3, acct, note, BUYBACK, amount, w3.eth.get_transaction_count(acct.address, "pending"), tx_delay)
    fn = bb.functions.sellNoteForQuote(amount, out * 99 // 100)
    why = simulate(fn, acct)
    if why:
        log(f"#{line_no} {acct.address}: buyback refused: {why}")
        return False
    send(w3, acct, fn, nonce)
    log(f"#{line_no} {acct.address}: buyback sold {amount / 1e18:,.0f} NOTE for {out / 1e6:,.2f} USDG")
    return True


# ---------------------------------------------------------------- Settle (feeds Omnichain)
def settle(w3, line_no, acct, tx_delay):
    """NoteCore.settle every struck (Live) position of the wallet that is not settled yet: it mints the
    COUPON / SHIELD legs (ERC-1155 on NoteLegs, what Omnichain moves) and refunds the unmatched part
    of the deposit. Anyone may settle any account; the app leaves it to the user."""
    from streak_guard import wallet_deposits  # local: streak_guard imports deposit/shield

    core = w3.eth.contract(address=CORE, abi=[
        {"type": "function", "name": "seriesStatus", "stateMutability": "view",
         "inputs": [{"type": "uint256"}], "outputs": [{"type": "uint8"}]},
        {"type": "function", "name": "getPosition", "stateMutability": "view",
         "inputs": [{"type": "uint256"}, {"type": "address"}],
         "outputs": [{"type": "tuple", "components": [{"type": "uint256"}] * 8 + [{"type": "bool"}]}]},
        {"type": "function", "name": "settle", "stateMutability": "nonpayable",
         "inputs": [{"type": "uint256"}, {"type": "address"}], "outputs": []}])
    usdg = contract(w3, TOKENS["USDG"], "erc20")
    todo = [sid for sid in sorted({r["sid"] for r in wallet_deposits(w3, acct.address)})
            if core.functions.seriesStatus(sid).call() == LIVE
            and not core.functions.getPosition(sid, acct.address).call()[8]]  # [8]: settled
    if not todo:
        log(f"#{line_no} {acct.address}: settle skip, nothing to settle")
        return False
    before = usdg.functions.balanceOf(acct.address).call()
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    done = []
    for sid in todo:
        fn = core.functions.settle(sid, acct.address)
        why = simulate(fn, acct)
        if why:
            log(f"#{line_no} {acct.address}: settle #{sid} refused: {why}")
            continue
        try:
            send(w3, acct, fn, nonce)
            nonce += 1
            done.append(f"#{sid}")
        except Exception as e:
            log(f"#{line_no} {acct.address}: settle #{sid} ERROR {str(e)[:150]}")
            nonce = w3.eth.get_transaction_count(acct.address, "pending")
        time.sleep(random.uniform(*tx_delay))
    refund = (usdg.functions.balanceOf(acct.address).call() - before) / 1e6
    log(f"#{line_no} {acct.address}: settled [{', '.join(done) or '-'}], refund {refund:,.2f} USDG")
    return bool(done)


# ---------------------------------------------------------------- Omnichain (3%)
def omnichain(w3, line_no, acct, pct, tx_delay):
    """Move pct % of one COUPON leg to Arbitrum Sepolia (leg units away x days; coupons keep
    accruing there). Only Live series can move. One leg per run, each leg once (legs_v2.txt), and
    the legs stay away: a round trip inside 24h earns nothing, and returning needs Arbitrum gas."""
    from streak_guard import wallet_deposits  # local: streak_guard imports deposit/shield

    core = w3.eth.contract(address=CORE, abi=[
        {"type": "function", "name": "seriesStatus", "stateMutability": "view",
         "inputs": [{"type": "uint256"}], "outputs": [{"type": "uint8"}]}])
    legs = contract(w3, NOTE_LEGS, "legs")
    vault = contract(w3, LEG_VAULT, "legvault")
    sent = {l.split(",")[1] for l in read_lines(LEGS_SENT) if l.startswith(acct.address + ",")}
    sids = sorted({r["sid"] for r in wallet_deposits(w3, acct.address) if r["side"] == "coupon"})
    options = []
    for sid in sids:
        leg = sid << 1  # | 0 = COUPON leg
        if str(leg) in sent or core.functions.seriesStatus(sid).call() != LIVE:
            continue
        units = legs.functions.balanceOf(acct.address, leg).call()
        if units:
            options.append((sid, leg, units))
    if not options:
        log(f"#{line_no} {acct.address}: omnichain skip, no live COUPON leg left to move")
        return False
    sid, leg, units = random.choice(options)
    amount = max(units * random.uniform(*pct) // 100, 1)
    amount = int(amount)
    fee = vault.functions.quoteSend(ARB_SEPOLIA_EID, leg, amount, acct.address, b"").call()[0]
    if w3.eth.get_balance(acct.address) < fee + Web3.to_wei(0.0003, "ether"):
        log(f"#{line_no} {acct.address}: omnichain skip, ETH below the {fee / 1e18:.5f} ETH message fee")
        return False
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    if not legs.functions.isApprovedForAll(acct.address, LEG_VAULT).call():
        send(w3, acct, legs.functions.setApprovalForAll(LEG_VAULT, True), nonce)
        nonce += 1
        time.sleep(random.uniform(*tx_delay))
    fn = vault.functions.send(ARB_SEPOLIA_EID, leg, amount, acct.address, b"")
    why = simulate(fn, acct, fee)
    if why:
        log(f"#{line_no} {acct.address}: omnichain refused: {why}")
        return False
    h = send(w3, acct, fn, nonce, value=fee)
    with FILE_LOCK, LEGS_SENT.open("a", encoding="utf-8") as f:
        f.write(f"{acct.address},{leg},{amount},{h}\n")
    log(f"#{line_no} {acct.address}: moved {amount} units of #{sid} COUPON leg to Arbitrum Sepolia "
        f"(fee {fee / 1e18:.5f} ETH)")
    return True


# ---------------------------------------------------------------- Vaults (5%)
def vault_list(w3):
    """Factory vaults on USDG that already hold assets. Shared by all threads, refreshed hourly.
    Read 16 at a time: the factory lists hundreds of vaults."""
    with _vaults_lock:
        if time.time() - _vaults["at"] < 3600:
            return _vaults["list"]
        factory = w3.eth.contract(address=VAULT_FACTORY, abi=FACTORY_ABI)
        count = factory.functions.vaultCount().call()
        addresses = []
        for start in range(0, count, 100):  # paged: one huge vaults() call can hit the RPC response limit
            addresses += factory.functions.vaults(start, min(100, count - start)).call()

        def usable(v):
            try:
                c = w3.eth.contract(address=v, abi=VAULT_ABI)
                return (c.functions.asset().call().lower() == TOKENS["USDG"].lower()
                        and c.functions.totalAssets().call() >= VAULT_MIN_ASSETS)
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=16) as pool:
            found = [v for v, ok in zip(addresses, pool.map(usable, addresses)) if ok]
        _vaults.update(at=time.time(), list=found)
        log(f"vaults with assets: {len(found)}")
        return found


def vault(w3, line_no, acct, pct, tx_delay):
    """Deposit pct % of the USDG balance into a Build vault (vault shares held x days, + the Vault quest).
    The first run picks a random vault, later runs top up the same one. Never withdraws."""
    usdg = contract(w3, TOKENS["USDG"], "erc20")
    amount = pct_of(usdg.functions.balanceOf(acct.address).call(), pct, 10**6)
    if amount < 10**6:
        log(f"#{line_no} {acct.address}: vault skip, not enough USDG")
        return False
    mine = [Web3.to_checksum_address(l.split(",")[1]) for l in read_lines(VAULTS_HELD) if l.startswith(acct.address + ",")]

    def candidates():  # the wallet's own vault first; the full list is read only if that one refuses
        yield from mine[-1:]
        yield from (a for a in random.sample(vault_list(w3), len(vault_list(w3))) if a not in mine[-1:])

    for address in candidates():  # a full or paused vault is skipped for the next one
        v = w3.eth.contract(address=address, abi=VAULT_ABI)
        room = v.functions.maxDeposit(acct.address).call()
        if room < 10**6:
            continue
        amount = min(amount, room // 10**6 * 10**6)
        if v.functions.previewDeposit(amount).call() == 0:
            continue
        nonce = approve(w3, acct, usdg, address, amount, w3.eth.get_transaction_count(acct.address, "pending"), tx_delay)
        fn = v.functions.deposit(amount, acct.address)
        why = simulate(fn, acct)
        if why:
            log(f"#{line_no} {acct.address}: vault {address[:10]} refused: {why}")
            continue
        h = send(w3, acct, fn, nonce)
        with FILE_LOCK, VAULTS_HELD.open("a", encoding="utf-8") as f:
            f.write(f"{acct.address},{address},{amount / 1e6},{h}\n")
        held = v.functions.convertToAssets(v.functions.balanceOf(acct.address).call()).call()
        log(f"#{line_no} {acct.address}: vault '{v.functions.name().call()}' +{amount / 1e6:,.0f} USDG, "
            f"holding {held / 1e6:,.2f} USDG")
        return True
    log(f"#{line_no} {acct.address}: vault skip, no vault takes deposits now")
    return False


# ---------------------------------------------------------------- Credit (Morpho markets on COUPON legs)
# LegWrapFactory wraps a series' COUPON leg (ERC-1155 on NoteLegs) 1:1 into an ERC-20 (nCm<SYM>-<id>) and opens a
# Morpho market for it: USDG lent against the wrapped leg, LLTV 38.5%. The site reads these markets but has no
# buttons for them yet, so the wallet talks to the contracts directly.
MORPHO = "0x44f0B631EDCbd06827aE11ad52f9f5cf4FF294d9"
LEG_WRAP_FACTORY = "0x6E94E9A100F6606c0B4855a716729bE2d3F2D24f"
CREDIT_LOG = BASE / "credit_v2.txt"  # "address,seriesId,action,amount,tx"
_U, _A, _B = {"type": "uint256"}, {"type": "address"}, {"type": "bytes32"}
_MP = {"type": "tuple", "components": [{"name": n, "type": t} for n, t in (
    ("loanToken", "address"), ("collateralToken", "address"), ("oracle", "address"), ("irm", "address"),
    ("lltv", "uint256"))]}


def _fn(name, inputs, outputs=(), view=False):
    return {"type": "function", "name": name, "stateMutability": "view" if view else "nonpayable",
            "inputs": list(inputs), "outputs": list(outputs)}


WRAP_FACTORY_ABI = [_fn("count", [], [_U], True), _fn("seriesAt", [_U], [_U], True),
                    _fn("marketParams", [_U], [_MP], True), _fn("marketOf", [_U], [_B], True)]
MORPHO_ABI = [
    _fn("market", [_B], [{"type": "uint128"}] * 6, True),
    _fn("position", [_B, _A], [_U, {"type": "uint128"}, {"type": "uint128"}], True),
    _fn("supply", [_MP, _U, _U, _A, {"type": "bytes"}], [_U, _U]),
    _fn("supplyCollateral", [_MP, _U, _A, {"type": "bytes"}]),
    _fn("borrow", [_MP, _U, _U, _A, _A], [_U, _U]),
]
WRAP_ABI = [_fn("wrap", [_U, _A], [_U]), _fn("symbol", [], [{"type": "string"}], True),
            _fn("balanceOf", [_A], [_U], True), _fn("allowance", [_A, _A], [_U], True),
            _fn("approve", [_A, _U], [{"type": "bool"}])]
ORACLE_ABI = [_fn("price", [], [_U], True)]
_markets = {"at": 0, "list": []}
_markets_lock = threading.Lock()


def credit_markets(w3):
    """[(seriesId, params, marketId)] of every Credit market. Shared by all threads, refreshed hourly."""
    with _markets_lock:
        if time.time() - _markets["at"] < 3600:
            return _markets["list"]
        f = w3.eth.contract(address=LEG_WRAP_FACTORY, abi=WRAP_FACTORY_ABI)
        found = []
        for i in range(f.functions.count().call()):
            sid = f.functions.seriesAt(i).call()
            found.append((sid, tuple(f.functions.marketParams(sid).call()), f.functions.marketOf(sid).call()))
        _markets.update(at=time.time(), list=found)
        return found


def _credit_log(acct, sid, action, amount, h):
    with FILE_LOCK, CREDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{acct.address},{sid},{action},{amount},{h}\n")


def _borrow(w3, line_no, acct, morpho, markets, wrap_pct, borrow_pct, tx_delay):
    """Wrap part of one COUPON leg, post it as collateral and borrow a little USDG. Returns what was done."""
    legs = contract(w3, NOTE_LEGS, "legs")
    options = []
    for sid, params, mid in markets:
        units = legs.functions.balanceOf(acct.address, sid << 1).call()  # | 0 = COUPON leg
        m = morpho.functions.market(mid).call()
        if units and m[0] - m[2] >= 10 * 10**6:  # the market has USDG left to lend
            options.append((sid, params, units, m[0] - m[2]))
    if not options:
        return "no COUPON leg with a market to borrow on"
    sid, params, units, room = random.choice(options)
    _, coll, oracle, _, lltv = params
    wrap = w3.eth.contract(address=coll, abi=WRAP_ABI)
    symbol = wrap.functions.symbol().call()
    shares = int(units * random.uniform(*wrap_pct) / 100)
    price = w3.eth.contract(address=oracle, abi=ORACLE_ABI).functions.price().call()
    max_borrow = shares * price // 10**36 * lltv // 10**18
    borrow = min(int(max_borrow * random.uniform(*borrow_pct) / 100), room // 2) // 10**4 * 10**4
    if borrow < 10**6:
        return f"#{sid} leg too small to borrow on"

    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    if not legs.functions.isApprovedForAll(acct.address, coll).call():  # wrap() pulls the leg itself
        send(w3, acct, legs.functions.setApprovalForAll(coll, True), nonce)
        nonce += 1
        time.sleep(random.uniform(*tx_delay))
    fn = wrap.functions.wrap(shares, acct.address)
    why = simulate(fn, acct)
    if why:
        return f"wrap #{sid} refused: {why}"
    _credit_log(acct, sid, "wrap", shares / 1e6, send(w3, acct, fn, nonce))
    time.sleep(random.uniform(*tx_delay))

    shares = wrap.functions.balanceOf(acct.address).call()
    nonce = approve(w3, acct, wrap, MORPHO, shares, w3.eth.get_transaction_count(acct.address, "pending"), tx_delay)
    fn = morpho.functions.supplyCollateral(params, shares, acct.address, b"")
    why = simulate(fn, acct)
    if why:
        return f"supplyCollateral #{sid} refused: {why} ({shares / 1e6:,.2f} {symbol} stay wrapped)"
    _credit_log(acct, sid, "collateral", shares / 1e6, send(w3, acct, fn, nonce))
    time.sleep(random.uniform(*tx_delay))

    fn = morpho.functions.borrow(params, borrow, 0, acct.address, acct.address)
    why = simulate(fn, acct)  # a stale price feed blocks borrowing for a while: the next run tries again
    if why:
        return f"collateral {shares / 1e6:,.2f} {symbol} posted, borrow refused: {why}"
    _credit_log(acct, sid, "borrow", borrow / 1e6, send(w3, acct, fn, w3.eth.get_transaction_count(acct.address, "pending")))
    return (f"borrowed {borrow / 1e6:,.2f} USDG on {shares / 1e6:,.2f} {symbol} "
            f"(LTV {borrow / max_borrow * lltv / 1e16:.1f}%, liquidation at {lltv / 1e16:.1f}%)")


def credit(w3, line_no, acct, supply_pct, wrap_pct, borrow_pct, tx_delay):
    """Use the Credit markets both ways, never withdrawing or repaying:
    - lend: supply_pct % of the USDG balance into a market (the same one on later runs);
    - borrow, once per wallet: wrap wrap_pct % of one COUPON leg, post it as collateral and borrow borrow_pct %
      of what the LLTV allows, so the loan-to-value stays far below the liquidation line. The loan stays open;
      a wallet with collateral but no loan (borrow refused earlier) borrows on its next run."""
    markets = credit_markets(w3)
    if not markets:
        log(f"#{line_no} {acct.address}: credit skip, no markets")
        return False
    morpho = w3.eth.contract(address=MORPHO, abi=MORPHO_ABI)
    usdg = contract(w3, TOKENS["USDG"], "erc20")
    positions = {sid: morpho.functions.position(mid, acct.address).call() for sid, _, mid in markets}
    done, acted = [], False

    amount = pct_of(usdg.functions.balanceOf(acct.address).call(), supply_pct, 10**6)
    if amount >= 10**6:
        lent = [m for m in markets if positions[m[0]][0]]
        sid, params, _ = random.choice(lent or markets)
        nonce = approve(w3, acct, usdg, MORPHO, amount, w3.eth.get_transaction_count(acct.address, "pending"), tx_delay)
        fn = morpho.functions.supply(params, amount, 0, acct.address, b"")
        why = simulate(fn, acct)
        if why:
            done.append(f"lend #{sid} refused: {why}")
        else:
            _credit_log(acct, sid, "supply", amount / 1e6, send(w3, acct, fn, nonce))
            done.append(f"lent {amount / 1e6:,.0f} USDG to #{sid}")
            acted = True
            time.sleep(random.uniform(*tx_delay))

    loan = next((s for s, p in positions.items() if p[1]), None)
    posted = next((m for m in markets if positions[m[0]][2]), None)
    if loan is not None:
        done.append(f"loan on #{loan} kept")
    elif posted:  # collateral from an earlier run whose borrow was refused: borrow on it now
        sid, params, _ = posted
        price = w3.eth.contract(address=params[2], abi=ORACLE_ABI).functions.price().call()
        borrow = int(positions[sid][2] * price // 10**36 * params[4] // 10**18
                     * random.uniform(*borrow_pct) / 100) // 10**4 * 10**4
        fn = morpho.functions.borrow(params, borrow, 0, acct.address, acct.address)
        why = simulate(fn, acct) if borrow >= 10**6 else "collateral too small"
        if why:
            done.append(f"borrow #{sid} refused: {why}")
        else:
            _credit_log(acct, sid, "borrow", borrow / 1e6,
                        send(w3, acct, fn, w3.eth.get_transaction_count(acct.address, "pending")))
            done.append(f"borrowed {borrow / 1e6:,.2f} USDG on #{sid}")
            acted = True
    else:
        result = _borrow(w3, line_no, acct, morpho, markets, wrap_pct, borrow_pct, tx_delay)
        done.append(result)
        acted |= result.startswith(("borrowed", "collateral"))
    log(f"#{line_no} {acct.address}: credit: {'; '.join(done)}")
    return acted


# ---------------------------------------------------------------- Governance proposals
# NoteGovernor (parameters) and UpgradeGovernor (upgrades): OpenZeppelin Governor on veNOTE votes, timestamp clock.
GOVERNORS = {"NoteGovernor": "0x8819B916639406B0be678F69443Ac6D39663d79B",
             "UpgradeGovernor": "0xEE46A35D6ECA2574C5662271e899E995a883F829"}
GOV_FROM_BLOCK = 127302759  # the pre-season deploy
GOV_LOG_STEP = 2_000_000
ACTIVE = 1
GOVERNOR_ABI = [
    {"anonymous": False, "type": "event", "name": "ProposalCreated", "inputs": [
        {"name": "proposalId", "type": "uint256", "indexed": False},
        {"name": "proposer", "type": "address", "indexed": False},
        {"name": "targets", "type": "address[]", "indexed": False},
        {"name": "values", "type": "uint256[]", "indexed": False},
        {"name": "signatures", "type": "string[]", "indexed": False},
        {"name": "calldatas", "type": "bytes[]", "indexed": False},
        {"name": "voteStart", "type": "uint256", "indexed": False},
        {"name": "voteEnd", "type": "uint256", "indexed": False},
        {"name": "description", "type": "string", "indexed": False}]},
    _fn("state", [_U], [{"type": "uint8"}], True),
    _fn("hasVoted", [_U, _A], [{"type": "bool"}], True),
    _fn("proposalSnapshot", [_U], [_U], True),
    _fn("getVotes", [_A, _U], [_U], True),
    _fn("castVote", [_U, {"type": "uint8"}], [_U]),
]
_proposals = {"at": 0, "list": []}
_proposals_lock = threading.Lock()


def active_proposals(w3):
    """[(governor, proposalId, title)] open for votes. Shared by all threads, refreshed every 10 minutes."""
    with _proposals_lock:
        if time.time() - _proposals["at"] < 600:
            return _proposals["list"]
        head = w3.eth.block_number
        found = []
        for name, address in GOVERNORS.items():
            g = w3.eth.contract(address=address, abi=GOVERNOR_ABI)
            for start in range(GOV_FROM_BLOCK, head + 1, GOV_LOG_STEP):
                end = min(start + GOV_LOG_STEP - 1, head)
                for lg in g.events.ProposalCreated().get_logs(from_block=start, to_block=end):
                    if g.functions.state(lg.args.proposalId).call() == ACTIVE:
                        title = lg.args.description.lstrip("# ").split("\n")[0][:60]
                        found.append((name, lg.args.proposalId, title))
        _proposals.update(at=time.time(), list=found)
        return found


def proposals(w3, line_no, acct, weights):
    """Vote on every open proposal the wallet has not voted on yet, with its veNOTE at the proposal's snapshot.
    The side is drawn per wallet and proposal from weights = (for, against, abstain)."""
    todo = active_proposals(w3)
    if not todo:
        log(f"#{line_no} {acct.address}: proposals skip, none open")
        return False
    voted = []
    for name, pid, title in todo:
        g = w3.eth.contract(address=GOVERNORS[name], abi=GOVERNOR_ABI)
        if g.functions.hasVoted(pid, acct.address).call():
            continue
        if not g.functions.getVotes(acct.address, g.functions.proposalSnapshot(pid).call()).call():
            log(f"#{line_no} {acct.address}: proposal '{title}' skip, no veNOTE at its snapshot")
            continue
        support = random.choices((1, 0, 2), weights=weights)[0]  # OpenZeppelin: 0 against, 1 for, 2 abstain
        fn = g.functions.castVote(pid, support)
        why = simulate(fn, acct)
        if why:
            log(f"#{line_no} {acct.address}: proposal '{title}' vote refused: {why}")
            continue
        send(w3, acct, fn, w3.eth.get_transaction_count(acct.address, "pending"))
        voted.append(f"{('against', 'for', 'abstain')[support]} '{title}'")
        time.sleep(random.uniform(1, 4))
    if voted:
        log(f"#{line_no} {acct.address}: voted {'; '.join(voted)}")
    return bool(voted)
