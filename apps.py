"""Pre-season apps beyond notes and bonds: Desk, Staking, Locks, Gauge votes, Buyback, Omnichain.

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
import time
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
LIVE = 2
WEEK = 7 * 86400


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
