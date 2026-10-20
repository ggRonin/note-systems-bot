"""SWAP: the Swap app (stock <-> USDG engine and its liquidity vaults), pre-season "swap" stream and its quests.

The engine prices every swap from a signed Chainlink Data Streams report: the site fetches it from its report
service and passes it into core.swap(). The bot does the same, directly like the RPC (the request carries only feed
ids, nothing about the wallet, and an account proxy that hiccups would only cost swaps). Markets (token, vaults, feed ids) come from swap_markets.json, copied from the site's Swap config;
listed / enabled / cut is read from chain on every run.

Per wallet and run (stream: swap volume + inventory supplied x days; quests in brackets):
- 1..N swaps: mostly buying a random market with USDG, sometimes selling part of a faucet stock
  [First Swap, Three Stocks; a swap while the market is in Discovery (stale print, e.g. weekends) = Closed Hours];
- top up stock inventory: part of one faucet stock into its Regular or Continuous vault, the same vault on later
  runs [Stock Inventory; never withdrawn, so it is held through the weekend = Through the Weekend];
- top up the Senior USDG vault [Senior USDG];
- Backstop, once per wallet: request a USDG deposit; every run tries to settle it (the contract settles only while
  every open market holds a fresh print) [Join the Backstop].
Nothing is ever withdrawn or redeemed. Every call is simulated before it is sent.
"""
import json
import random
import time
from pathlib import Path

import requests
from eth_abi import decode
from web3 import Web3

from apps import approve, contract, simulate
from deposit import send
from faucet import TOKENS
from register import FILE_LOCK, UA, log, read_lines

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "swap_markets.json").read_text(encoding="utf-8"))
MARKETS = CFG["markets"]
SWAP_LOG = BASE / "swap_v2.txt"  # "address,kind,symbol,amount,detail,tx"
DISCOVERY = 2
REGIMES = ["Cold", "Anchored", "Discovery", "Safety"]
DEADLINE = 600  # seconds, like the site
_SWAP = "(address,bool,bool,uint256,uint256,address,uint40)"  # stock, stockIn, exactIn, amount, limit, recipient, deadline
_QUOTE = "(uint256,uint256,uint256,uint256,uint256,int256,uint8)"  # amountIn, amountOut, stockAmount, mid, fee, shortfall, regime
_MARKET = "(address,uint8,bool,bool,bool,address,uint64" + ",uint128" * 7 + ",uint40" + ",uint128" * 5 + ")"


def _abi(sig):
    """'name(type,...)->(type,...)' or with a trailing ' view' -> one ABI function entry."""
    view = sig.endswith(" view")
    sig = sig.removesuffix(" view")
    head, _, outs = sig.partition("->")
    name, _, ins = head.partition("(")

    def split(s):
        out, depth, cur = [], 0, ""
        for ch in s:
            if ch == "," and depth == 0:
                out.append(cur)
                cur = ""
                continue
            depth += ch == "("
            depth -= ch == ")"
            cur += ch
        return [x for x in out + [cur] if x]

    def typ(t):
        if t.startswith("("):
            return {"type": "tuple", "components": [typ(x) for x in split(t[1:-1])]}
        return {"type": t}

    return {"type": "function", "name": name, "stateMutability": "view" if view else "nonpayable",
            "inputs": [typ(t) for t in split(ins[:-1])], "outputs": [typ(t) for t in split(outs[1:-1])]}


CORE_ABI = [_abi(f"swap({_SWAP},bytes)->(uint256,uint256)"), _abi(f"marketOf(address)->({_MARKET}) view")]
LENS_ABI = [_abi(f"quoteWithReport({_SWAP},bytes)->({_QUOTE})")]
VAULT_ABI = [_abi("deposit(uint256,address)->(uint256)"), _abi("balanceOf(address)->(uint256) view"),
             _abi("convertToAssets(uint256)->(uint256) view")]
BACKSTOP_ABI = [_abi("requestDeposit(uint256)->(uint256)"), _abi("settleDeposit(uint256)->(uint256)"),
                _abi("depositCount()->(uint256) view"), _abi("depositOf(uint256)->((address,uint128,bool)) view")]


ERRORS = {Web3.keccak(text=e)[:4].hex().removeprefix("0x"): e.split("(")[0] for e in (
    "BadParams()", "BadPrice()", "BoundExceeded()", "CapitalLimit(uint256,uint256)", "ExceedsLendable(uint256,uint256)",
    "Expired()", "InsufficientJunior(uint256,uint256)", "InventoryUnavailable(uint256,uint256)", "MarketCold(address)",
    "MarketCut(address)", "MarketHalted(address)", "NoLiquidity()", "NotCovered(uint256,uint256)", "NotFresh()",
    "OutOfDomain()", "OutsideBand()", "QueueWaiting()", "QuoteUnavailable(uint256,uint256)", "RegularWindowOpen()",
    "ReportExpired()", "ReportNotValid()", "RiskBudget(uint256,uint256)", "Slippage(uint256,uint256)", "Unbacked()",
    "WrongFeed(bytes32)", "WrongStream(bytes32,uint32)", "WrongTier(address)", "ZeroAmount()")}


def _why(text):
    """A revert reason with the engine's custom error named (CapitalLimit, NotFresh, ...) instead of raw data."""
    for sel, name in ERRORS.items():
        if sel in text:
            return name
    return text[:120]


def _log(acct, kind, symbol, amount, detail, h):
    with FILE_LOCK, SWAP_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{acct.address},{kind},{symbol},{amount},{detail},{h}\n")


def _history(acct):
    return [l.split(",") for l in read_lines(SWAP_LOG) if l.startswith(acct.address + ",")]


def _round_sig(x, digits):
    """Round down to `digits` significant digits (an amount typed by hand)."""
    if x <= 0:
        return 0
    step = 10 ** max(len(str(int(x))) - digits, 0)
    return int(x) // step * step


# ---------------------------------------------------------------- reports
def report(session, symbol):
    """The canonical signed report for the market's current session, as the site picks it: the regular stream's
    status says which of regular / extended / overnight is live. Returns (fullReport bytes, expiresAt)."""
    feeds = MARKETS[symbol]["feeds"]
    for attempt in range(4):  # the report service drops a connection now and then
        try:
            r = session.get(CFG["reports"] + "/api/v1/reports/bulk", params={"feedIDs": ",".join(feeds)}, timeout=30)
            r.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(3 + 3 * attempt)
    parsed = {}
    for rep in r.json()["reports"]:
        raw = bytes.fromhex(rep["fullReport"][2:])
        _, blob, *_ = decode(["bytes32[3]", "bytes", "bytes32[]", "bytes32[]", "bytes32"], raw)
        fields = decode(["bytes32", "uint32", "uint32", "uint192", "uint192", "uint32", "int192", "uint64", "int192",
                         "int192", "int192", "int192", "int192", "uint32"], blob)
        parsed[rep["feedID"].lower()] = (raw, fields[5], fields[13])  # report, expiresAt, market status
    status = parsed.get(feeds[0].lower(), next(iter(parsed.values()), (b"", 0, 0)))[2]
    want = feeds[0] if status == 2 else feeds[1] if status in (1, 3) else feeds[2] if status == 4 else feeds[0]
    if want.lower() not in parsed:
        raise RuntimeError(f"{symbol}: report service has no canonical stream")
    raw, expires, _ = parsed[want.lower()]
    return raw, expires


# ---------------------------------------------------------------- swaps
def _swap_once(w3, acct, session, core, lens, symbol, stock_in, amount, slippage_bps):
    """Quote and send one exact-in swap. Returns a log line or raises RuntimeError with the reason."""
    token = Web3.to_checksum_address(MARKETS[symbol]["token"])
    rep, _ = report(session, symbol)
    params = (token, stock_in, True, amount, 0, acct.address, int(time.time()) + DEADLINE)
    q = None
    for data in (rep, b""):  # in Discovery the engine prices from its own last print: the empty report is enough
        try:
            q = lens.functions.quoteWithReport(params, data).call({"from": acct.address})
            rep = data
            break
        except Exception as e:
            why = _why(str(e))
    if q is None:
        raise RuntimeError(f"quote refused: {why}")
    limit = q[1] * (10_000 - slippage_bps) // 10_000
    params = (token, stock_in, True, amount, limit, acct.address, int(time.time()) + DEADLINE)
    pay = token if stock_in else CFG["usdg"]
    approve(w3, acct, contract(w3, pay, "erc20"), core.address, amount,
            w3.eth.get_transaction_count(acct.address, "pending"), (1, 3))
    fn = core.functions.swap(params, rep)
    why = simulate(fn, acct)
    if why:
        raise RuntimeError(f"swap refused: {_why(why)}")
    h = send(w3, acct, fn, w3.eth.get_transaction_count(acct.address, "pending"))
    regime = REGIMES[q[6]] if q[6] < len(REGIMES) else str(q[6])
    if stock_in:
        text = f"sold {amount / 1e18:.4g} {symbol} for {q[1] / 1e6:,.2f} USDG"
        _log(acct, "sell", symbol, amount / 1e18, f"{q[1] / 1e6}:{regime}", h)
    else:
        text = f"bought {q[1] / 1e18:.4g} {symbol} for {amount / 1e6:,.2f} USDG"
        _log(acct, "buy", symbol, amount / 1e6, f"{q[1] / 1e18}:{regime}", h)
    return text + (" (Discovery)" if q[6] == DISCOVERY else "")


def swaps(w3, acct, session, core, lens, open_markets, count, usdg_range, sell_pct, sell_chance, slippage_bps,
          tx_delay):
    usdg = contract(w3, CFG["usdg"], "erc20")
    traded = {row[2] for row in _history(acct) if row[1] in ("buy", "sell")}
    held = {s: contract(w3, MARKETS[s]["token"], "erc20").functions.balanceOf(acct.address).call()
            for s in TOKENS if s in open_markets}
    done = []
    def buy():
        fresh = [s for s in open_markets if s not in traded]  # new stocks first: the Three Stocks quest
        symbol = random.choice(fresh if fresh and len(traded) < 3 else open_markets)
        amount = int(random.uniform(*usdg_range)) * 10**6
        if usdg.functions.balanceOf(acct.address).call() < amount:
            raise RuntimeError("not enough USDG to buy")
        return symbol, False, amount

    for _ in range(random.randint(*count)):
        sellable = [s for s, b in held.items() if b >= 10**15]
        plans = []
        if sellable and random.random() < sell_chance:
            symbol = random.choice(sellable)
            plans.append((symbol, True, _round_sig(held[symbol] * random.uniform(*sell_pct) / 100, random.choice([2, 3]))))
        plans.append(None)  # a buy: the first plan, or the fallback when the engine refuses the sell (CapitalLimit)
        for plan in plans:
            try:
                symbol, stock_in, amount = plan or buy()
                if not amount:
                    continue
                done.append(_swap_once(w3, acct, session, core, lens, symbol, stock_in, amount, slippage_bps))
                traded.add(symbol)
                if stock_in:
                    held[symbol] -= amount
                break
            except (RuntimeError, requests.RequestException) as e:
                done.append(f"{plan[0] + ': ' if plan else ''}{str(e)[:120]}")
        time.sleep(random.uniform(*tx_delay))
    return done


# ---------------------------------------------------------------- liquidity
def inventory(w3, acct, open_markets, pct, tx_delay):
    """Part of one faucet stock into its Regular or Continuous vault; later runs top up the same vault."""
    mine = [row for row in _history(acct) if row[1] in ("regular", "continuous")]
    if mine:
        kind, symbol = mine[-1][1], mine[-1][2]
    else:
        held = [s for s in TOKENS if s in open_markets
                and contract(w3, MARKETS[s]["token"], "erc20").functions.balanceOf(acct.address).call() >= 10**16]
        if not held:
            return "inventory skip, no faucet stock to supply"
        kind, symbol = random.choice(["regular", "continuous"]), random.choice(held)
    if symbol not in open_markets:
        return f"inventory skip, {symbol} market closed"
    token = contract(w3, MARKETS[symbol]["token"], "erc20")
    amount = _round_sig(token.functions.balanceOf(acct.address).call() * random.uniform(*pct) / 100, 2)
    if amount < 10**15:
        return f"inventory skip, no {symbol} left"
    vault_addr = Web3.to_checksum_address(MARKETS[symbol][kind + "Vault"])
    vault = w3.eth.contract(address=vault_addr, abi=VAULT_ABI)
    nonce = approve(w3, acct, token, vault_addr, amount, w3.eth.get_transaction_count(acct.address, "pending"), tx_delay)
    fn = vault.functions.deposit(amount, acct.address)
    why = simulate(fn, acct)
    if why:
        return f"inventory {symbol} {kind} refused: {why}"
    _log(acct, kind, symbol, amount / 1e18, "", send(w3, acct, fn, nonce))
    held = vault.functions.convertToAssets(vault.functions.balanceOf(acct.address).call()).call()
    return f"+{amount / 1e18:.4g} {symbol} into its {kind} vault (holding {held / 1e18:.4g})"


def senior(w3, acct, pct, tx_delay):
    usdg = contract(w3, CFG["usdg"], "erc20")
    amount = int(usdg.functions.balanceOf(acct.address).call() * random.uniform(*pct) / 100) // 10**6 * 10**6
    if amount < 10**6:
        return "senior skip, not enough USDG"
    vault_addr = Web3.to_checksum_address(CFG["quoteVault"])
    vault = w3.eth.contract(address=vault_addr, abi=VAULT_ABI)
    nonce = approve(w3, acct, usdg, vault_addr, amount, w3.eth.get_transaction_count(acct.address, "pending"), tx_delay)
    fn = vault.functions.deposit(amount, acct.address)
    why = simulate(fn, acct)
    if why:
        return f"senior refused: {why}"
    _log(acct, "senior", "USDG", amount / 1e6, "", send(w3, acct, fn, nonce))
    return f"+{amount / 1e6:,.0f} USDG into the Senior vault"


def backstop(w3, acct, usdg_range, tx_delay):
    """Request one USDG deposit per wallet, then settle it on a later run once the contract allows it."""
    b = w3.eth.contract(address=Web3.to_checksum_address(CFG["backstop"]), abi=BACKSTOP_ABI)
    rows = _history(acct)
    requested = [int(r[4]) for r in rows if r[1] == "backstop-request" and r[4].isdigit()]
    if not requested and any(r[1] == "backstop-request" for r in rows):
        return "backstop requested earlier, its deposit id was not found"
    settled = {int(r[4]) for r in rows if r[1] == "backstop-settle"}
    if not requested:
        usdg = contract(w3, CFG["usdg"], "erc20")
        amount = int(random.uniform(*usdg_range)) * 10**6
        if usdg.functions.balanceOf(acct.address).call() < amount:
            return "backstop skip, not enough USDG"
        nonce = approve(w3, acct, usdg, b.address, amount, w3.eth.get_transaction_count(acct.address, "pending"),
                        tx_delay)
        fn = b.functions.requestDeposit(amount)
        why = simulate(fn, acct)
        if why:
            return f"backstop request refused: {why}"
        h = send(w3, acct, fn, nonce)
        dep_id = None
        for _ in range(5):  # the RPC pool may answer from a node a block behind
            count = b.functions.depositCount().call()
            dep_id = next((i for i in range(count - 1, max(count - 50, 0) - 1, -1)
                           if b.functions.depositOf(i).call()[0] == acct.address), None)
            if dep_id is not None:
                break
            time.sleep(3)
        _log(acct, "backstop-request", "USDG", amount / 1e6, dep_id, h)
        return f"backstop deposit #{dep_id} of {amount / 1e6:,.0f} USDG requested (settles on a later run)"
    open_ids = [i for i in requested if i not in settled and not b.functions.depositOf(i).call()[2]]
    if not open_ids:
        return "backstop joined"
    for dep_id in open_ids:
        fn = b.functions.settleDeposit(dep_id)
        why = simulate(fn, acct)
        if why:
            return f"backstop #{dep_id} not settleable yet ({_why(why)})"
        _log(acct, "backstop-settle", "USDG", 0, dep_id, send(w3, acct, fn, w3.eth.get_transaction_count(acct.address, "pending")))
        time.sleep(random.uniform(*tx_delay))
    return f"backstop deposit settled ({', '.join(f'#{i}' for i in open_ids)})"


# ---------------------------------------------------------------- per account
def run(w3, line_no, acct, cfg, tx_delay):
    """cfg: the SWAP_* settings from config.py as a dict. Returns True when anything was sent."""
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Origin": "https://note.systems", "Referer": "https://note.systems/app/"})
    core = w3.eth.contract(address=Web3.to_checksum_address(CFG["core"]), abi=CORE_ABI)
    lens = w3.eth.contract(address=Web3.to_checksum_address(CFG["lens"]), abi=LENS_ABI)
    open_markets = []
    for symbol in random.sample(list(MARKETS), min(len(MARKETS), 40)) + [s for s in TOKENS if s in MARKETS]:
        if symbol in open_markets:
            continue
        m = core.functions.marketOf(Web3.to_checksum_address(MARKETS[symbol]["token"])).call()
        if m[2] and m[3] and not m[4]:  # listed, enabled, not cut
            open_markets.append(symbol)
    if not open_markets:
        log(f"#{line_no} {acct.address}: swap skip, no open markets")
        return False
    parts = swaps(w3, acct, session, core, lens, open_markets, cfg["count"], cfg["usdg"], cfg["sell_pct"],
                  cfg["sell_chance"], cfg["slippage_bps"], tx_delay)
    for step in (lambda: inventory(w3, acct, open_markets, cfg["inventory_pct"], tx_delay),
                 lambda: senior(w3, acct, cfg["senior_pct"], tx_delay),
                 lambda: backstop(w3, acct, cfg["backstop_usdg"], tx_delay)):
        try:
            parts.append(step())
        except Exception as e:
            parts.append(f"ERROR {str(e)[:120]}")
        time.sleep(random.uniform(*tx_delay))
    log(f"#{line_no} {acct.address}: swap: " + "; ".join(parts))
    return any(p.startswith(("bought", "sold", "+", "backstop deposit")) for p in parts)
