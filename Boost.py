"""Boost: mass sign-in + testnet faucet for the accounts in Boost.txt.

Boost.txt: one account per line, "seed phrase,user:pass@host:port" (like accounts.txt, no nick).
A line may be just the seed phrase: LOGIN then gives it the next proxy from Proxy.txt.
Addresses and keys are derived from the seeds at start, in parallel on all CPU cores.
Usage: python Boost.py

- SEND_ETH: splits the ETH of the first Boost account (minus ETH_RESERVE) evenly across
  the other Boost accounts not funded yet (boost_funded.txt). Runs first, to the end, before
  login/faucet start. Two-level fan-out: the funder pays HUBS hubs, each hub pays its chunk.
- LOGIN: SIWE sign-in through the account's proxy, exactly 2 requests (nonce + login),
  nothing else, to save proxy traffic. A failing proxy is replaced by the next line of
  Proxy.txt (removed from there) and written back into Boost.txt. Done: boost_done.txt.
- FAUCET: 1,000 USDG + 10 of each stock token per account, direct RPC (no proxy).
  Tokens still on cooldown revert at gas estimation and are skipped, so no extra calls.
"""
import os
import queue
import random
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from faucet import ABI as ERC20_ABI, CHAIN_ID, TOKENS
from rpc import make_w3
from register import (API, API_HEADERS, ATTEMPTS, BROWSER_HEADERS, FILE_LOCK, Blocked, ProxyError,
                      json_of, log, next_spare_proxy, proxy_dict, read_lines)

# ---------------------------------------------------------------- settings (1 = on, 0 = off)
SEND_ETH = 0              # split the ETH of the first Boost account across the other Boost accounts
LOGIN = 0                 # sign in on note.systems/community
FAUCET = 1                # claim test tokens
SEND_TOKENS = 0           # send all USDG + stock tokens to the accounts.txt wallets (Boost split evenly)
THREADS = 50              # parallel accounts
HUBS = 30                 # ETH split: parallel senders (the funder pays hubs, hubs pay the rest)
ETH_RESERVE = 0.001       # ETH kept on the funding (first Boost) account
DELAY_BETWEEN_TX = (0.3, 1)  # seconds between faucet claims of one account
# ----------------------------------------------------------------

Account.enable_unaudited_hdwallet_features()

BASE = Path(__file__).resolve().parent
BOOST = BASE / "Boost.txt"
DONE = BASE / "boost_done.txt"
FUNDED = BASE / "boost_funded.txt"
HUBS_FILE = BASE / "boost_hubs.txt"  # hubs of earlier runs: their leftover ETH is spent first
TRANSFER_GAS = 60_000     # Arbitrum Orbit: 21000 + L1 data; the real value is estimated at start

_records = []             # Boost.txt accounts: seed_phrase, proxy + derived address/key
_local = threading.local()


def w3():
    """One RPC connection per thread (rate-limited, two endpoints, retries on 429)."""
    if not hasattr(_local, "w3"):
        _local.w3 = make_w3()
    return _local.w3


def derive(seed):
    """seed -> (address, private key hex). Runs in worker processes."""
    a = Account.from_mnemonic(seed)
    return a.address, a.key.hex()


def load():
    """Read Boost.txt ("seed,proxy" lines) and derive every account's address and key."""
    for n, line in enumerate(BOOST.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        seed, _, proxy = (p.strip() for p in line.partition(","))
        if len(seed.split()) not in (12, 15, 18, 21, 24):
            raise RuntimeError(f"Boost.txt line {n}: expected 'seed phrase,proxy'")
        _records.append({"seed_phrase": seed, "proxy": proxy})
    with ProcessPoolExecutor() as pool:
        derived = pool.map(derive, [r["seed_phrase"] for r in _records], chunksize=200)
        for r, (address, key) in zip(_records, derived):
            r["address"], r["key"] = address, key


def account(i):
    return Account.from_key(_records[i]["key"])


def mark(path, address):
    with FILE_LOCK, path.open("a", encoding="utf-8") as f:
        f.write(address + "\n")


def save_proxy(i, proxy):
    """Rewrite only line i of Boost.txt, reading the file from disk (never from memory)."""
    with FILE_LOCK:
        lines = [l for l in BOOST.read_text(encoding="utf-8").splitlines() if l.strip()]
        seed = lines[i].split(",", 1)[0].strip()
        if seed != _records[i]["seed_phrase"]:
            raise RuntimeError(f"Boost.txt line {i + 1} does not match the loaded account, not writing")
        lines[i] = f"{seed},{proxy}"
        tmp = BOOST.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if len(tmp.read_text(encoding="utf-8").splitlines()) != len(lines):
            raise RuntimeError("Boost.txt rewrite check failed, not replacing")
        os.replace(tmp, BOOST)


def replace_proxy(i, reason):
    proxy = next_spare_proxy()
    if proxy is None:
        raise RuntimeError("Proxy.txt is empty")
    with FILE_LOCK:
        _records[i]["proxy"] = proxy
        save_proxy(i, proxy)
    log(f"[{i + 1}] {reason}, new proxy {proxy.split('@')[-1]}")
    return proxy


# ---------------------------------------------------------------- login (2 requests)
def sign_in(acct, proxy):
    s = requests.Session()
    s.proxies = proxy_dict(proxy)
    s.headers.update({**BROWSER_HEADERS, **API_HEADERS})
    address = acct.address.lower()
    try:
        n = json_of(s.get(API + "nonce.php", params={"address": address}, timeout=30))
        message = n.get("message", "")
        if not message.startswith("note.systems wants you to sign in with your Ethereum account:"):
            raise RuntimeError(f"unexpected sign-in message: {n}")
        sig = acct.sign_message(encode_defunct(text=message)).signature.hex()
        sig = sig if sig.startswith("0x") else "0x" + sig
        time.sleep(random.uniform(1, 3))
        data = json_of(s.post(API + "login.php", timeout=30,
                              json={"address": address, "message": message, "signature": sig}))
        if not data.get("ok"):
            raise RuntimeError(f"login failed: {data}")
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout,
            requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
        raise ProxyError(type(e).__name__)
    except requests.exceptions.ReadTimeout:
        raise ProxyError("read timeout")


def login(i, acct):
    # a seed-only line has no proxy yet: take one from Proxy.txt (removed there, written into Boost.txt)
    proxy = _records[i]["proxy"] or replace_proxy(i, "no proxy")
    for attempt in range(1, ATTEMPTS + 1):
        try:
            sign_in(acct, proxy)
            mark(DONE, acct.address)
            return True
        except ProxyError as e:
            proxy = replace_proxy(i, f"proxy failed ({e})")
        except Blocked as e:
            time.sleep(20 * attempt + random.uniform(0, 10))
            if attempt >= 2:
                proxy = replace_proxy(i, f"blocked ({e})")
        except RuntimeError as e:
            log(f"[{i + 1}] {e}, attempt {attempt}/{ATTEMPTS}")
            time.sleep(5)
    raise RuntimeError(f"login gave up after {ATTEMPTS} attempts")


# ---------------------------------------------------------------- faucet (direct RPC)
FAUCET_GAS = 130_000        # a claim uses ~85k gas; fixed limit saves an estimate call per tx
FAUCET_DATA = Web3.keccak(text="faucet()")[:4]
_gas_price = {"value": 0, "at": 0}


def gas_price():
    """Shared by all threads, refreshed once a minute (saves a call per tx)."""
    if time.time() - _gas_price["at"] > 60:
        _gas_price.update(value=w3().eth.gas_price * 2, at=time.time())
    return _gas_price["value"]


def claim(acct):
    """All 9 faucet claims in one burst with consecutive nonces, without waiting for
    receipts: 11 RPC calls per account (cooldown, nonce, 9 sends)."""
    web3 = w3()
    usdg = web3.eth.contract(address=TOKENS["USDG"], abi=ERC20_ABI)
    if usdg.functions.faucetCooldownRemaining(acct.address).call():
        return [], None, None  # all tokens are claimed together, so their cooldowns match
    nonce = web3.eth.get_transaction_count(acct.address, "pending")
    fee = gas_price()
    sent = []
    for sym, addr in TOKENS.items():
        tx = {"chainId": CHAIN_ID, "nonce": nonce, "to": addr, "data": FAUCET_DATA, "value": 0,
              "gas": FAUCET_GAS, "maxFeePerGas": fee, "maxPriorityFeePerGas": 0}
        try:
            sent.append((sym, web3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)))
        except Exception as e:
            if "insufficient funds" in str(e):
                break
            raise
        nonce += 1
        time.sleep(random.uniform(*DELAY_BETWEEN_TX))
    # fire and forget: nothing later depends on the result; a failed claim simply
    # shows up as "no cooldown" on the next run and is claimed again
    claimed = [sym for sym, _ in sent]
    return claimed, None if len(sent) == len(TOKENS) else "no ETH for gas", nonce


# ---------------------------------------------------------------- send tokens to accounts.txt wallets
MULTICALL = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL_ABI = [{"type": "function", "name": "aggregate3", "stateMutability": "payable",
                  "inputs": [{"name": "calls", "type": "tuple[]", "components": [
                      {"name": "target", "type": "address"}, {"name": "allowFailure", "type": "bool"},
                      {"name": "callData", "type": "bytes"}]}],
                  "outputs": [{"name": "returnData", "type": "tuple[]", "components": [
                      {"name": "success", "type": "bool"}, {"name": "returnData", "type": "bytes"}]}]}]
TRANSFER_TOKEN_GAS = 80_000   # a transfer uses ~49.5k gas
BALANCE_OF = Web3.keccak(text="balanceOf(address)")[:4]
TRANSFER = Web3.keccak(text="transfer(address,uint256)")[:4]
FAUCET_AMOUNT = {"USDG": 1_000 * 10**6, **{s: 10 * 10**18 for s in TOKENS if s != "USDG"}}
_mains = []   # accounts.txt addresses (targets)


_mains_lock = threading.Lock()


def main_wallets():
    with _mains_lock:  # built once: without the lock parallel threads each appended the list
        if not _mains:
            _mains.extend(Account.from_mnemonic(line.split(",", 1)[0].strip()).address
                          for line in read_lines(BASE / "accounts.txt"))
        return _mains


def target_of(i):
    """Boost accounts are split into len(accounts.txt) contiguous groups, one wallet each."""
    mains = main_wallets()
    return i * len(mains) // len(_records), mains[i * len(mains) // len(_records)]


def send_tokens(i, acct, claimed, nonce):
    """Send all USDG + stock tokens to this account's accounts.txt wallet. One multicall for the
    balances, then fire-and-forget transfers right after the faucet claims (next nonces)."""
    web3 = w3()
    mc = web3.eth.contract(address=MULTICALL, abi=MULTICALL_ABI)
    data = BALANCE_OF + bytes(12) + bytes.fromhex(acct.address[2:])
    res = mc.functions.aggregate3([(addr, True, data) for addr in TOKENS.values()]).call()
    amounts = {}
    for (sym, _), (ok, ret) in zip(TOKENS.items(), res):
        amount = (int.from_bytes(ret, "big") if ok and ret else 0) + (FAUCET_AMOUNT[sym] if sym in claimed else 0)
        if amount:
            amounts[sym] = amount
    k, to = target_of(i)
    if not amounts:
        return f"send: nothing to send -> #{k + 1}"
    if nonce is None:
        nonce = web3.eth.get_transaction_count(acct.address, "pending")
    fee, sent = gas_price(), []
    to_word = bytes(12) + bytes.fromhex(to[2:])
    for sym, amount in amounts.items():
        tx = {"chainId": CHAIN_ID, "nonce": nonce, "to": TOKENS[sym], "value": 0,
              "data": TRANSFER + to_word + amount.to_bytes(32, "big"),
              "gas": TRANSFER_TOKEN_GAS, "maxFeePerGas": fee, "maxPriorityFeePerGas": 0}
        try:
            web3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
        except Exception as e:
            if "insufficient funds" in str(e):
                return f"send -> #{k + 1}: {', '.join(sent) or '-'} (no ETH for gas)"
            raise
        sent.append(f"{sym} {amount / (10**6 if sym == 'USDG' else 10**18):g}")
        nonce += 1
        time.sleep(random.uniform(*DELAY_BETWEEN_TX))
    return f"send -> #{k + 1} {to[:8]}..: {', '.join(sent)}"


# ---------------------------------------------------------------- per account
def process(i, done):
    acct = account(i)
    parts = []
    if LOGIN:
        if acct.address in done:
            parts.append("login: done before")
        else:
            login(i, acct)
            parts.append("login: ok")
    claimed, nonce = [], None
    if FAUCET:
        claimed, err, nonce = claim(acct)
        parts.append(f"faucet sent: {', '.join(claimed) or 'nothing (cooldown)'}" + (f" ({err})" if err else ""))
    if SEND_TOKENS:
        parts.append(send_tokens(i, acct, claimed, nonce))
    log(f"[{i + 1}] {acct.address}: " + " | ".join(parts))


def worker(q, done, stats):
    while True:
        i = q.get()
        if i is None:
            return
        try:
            process(i, done)
            stats["ok"] += 1
        except Exception as e:
            stats["err"] += 1
            log(f"[{i + 1}] ERROR {str(e)[:200]}")
            if "Proxy.txt is empty" in str(e):
                stats["stop"] = True


# ---------------------------------------------------------------- ETH split
def fund(stop_after):
    """Phase 1: give every unfunded Boost account an equal share of all available ETH.

    Senders: hubs left over from an earlier run (boost_hubs.txt) spend their spare ETH first,
    then the funder (first Boost account) pays new hubs and each new hub pays its chunk.
    """
    web3 = w3()
    funder = account(0)
    funded = set(read_lines(FUNDED))
    todo = [i for i, r in enumerate(_records) if i > 0 and r["address"].lower() not in funded]
    if not todo:
        log("ETH: every Boost account is funded already")
        return
    index = {r["address"].lower(): i for i, r in enumerate(_records)}

    gas_price = web3.eth.gas_price * 2
    gas = int(web3.eth.estimate_gas({"from": funder.address, "to": Web3.to_checksum_address(
        _records[todo[0]]["address"]), "value": 1}) * 1.3) or TRANSFER_GAS
    fee = gas * gas_price
    keep = Web3.to_wei(0.00003, "ether")  # what a hub keeps for itself (its own share + faucet gas)

    old_hubs = [index[a] for a in read_lines(HUBS_FILE) if a in index]
    hub_bal = {h: web3.eth.get_balance(Web3.to_checksum_address(_records[h]["address"])) for h in old_hubs}
    funder_bal = web3.eth.get_balance(funder.address)
    spare_hubs = sum(max(b - keep, 0) for b in hub_bal.values())
    spare_funder = max(funder_bal - Web3.to_wei(ETH_RESERVE, "ether"), 0)
    share = ((spare_hubs + spare_funder) // len(todo) - fee) // 10**9 * 10**9
    per = share + fee
    log(f"ETH: funder {Web3.from_wei(funder_bal, 'ether')} ETH + {len(old_hubs)} old hubs "
        f"{Web3.from_wei(spare_hubs, 'ether')} ETH -> {len(todo)} accounts x {Web3.from_wei(max(share, 0), 'ether')} ETH")
    if share < Web3.to_wei(0.000008, "ether"):  # 9 faucet claims cost ~0.000007 ETH at 0.01 gwei
        log("ETH: share too small for faucet gas, funding skipped")
        return

    sent, lock, threads = {"n": 0}, threading.Lock(), []
    eth_share = Web3.from_wei(share, "ether")

    def paid(i, via):
        mark(FUNDED, _records[i]["address"].lower())
        with lock:
            sent["n"] += 1
            n = sent["n"]
        log(f"ETH: [{i + 1}] {_records[i]['address']} +{eth_share} ETH (from {via}) | {n}/{len(todo)}")

    def run_hub(h, chunk):
        try:
            hub = account(h)
            transfer_many(hub, [(i, share) for i in chunk], gas, gas_price, stop_after,
                          lambda i: paid(i, f"hub [{h + 1}]"))
            log(f"ETH: hub [{h + 1}] finished its {len(chunk)} transfers")
        except Exception as e:  # never let a hub thread die silently
            log(f"ETH: hub [{h + 1}] stopped: {str(e)[:150]}")

    def start_hub(h, chunk):
        t = threading.Thread(target=run_hub, args=(h, chunk), daemon=True)
        t.start()
        threads.append(t)

    # 1) old hubs spend what they already hold
    rest = list(todo)
    for h in old_hubs:
        n = max(hub_bal[h] - keep, 0) // per
        if n and rest:
            chunk, rest = rest[:n], rest[n:]
            log(f"ETH: old hub [{h + 1}] pays {len(chunk)} accounts from its balance")
            start_hub(h, chunk)

    # 2) the funder pays new hubs for the rest; a new hub keeps its own share and pays its chunk
    if rest:
        hubs = max(1, min(HUBS, len(rest)))
        chunks = [rest[k::hubs] for k in range(hubs)]
        lump = {c[0]: share + (len(c) - 1) * per for c in chunks}

        def hub_paid(i):
            with FILE_LOCK, HUBS_FILE.open("a", encoding="utf-8") as f:
                f.write(_records[i]["address"].lower() + "\n")
            paid(i, "funder")
            log(f"ETH: new hub [{i + 1}] got {Web3.from_wei(lump[i], 'ether')} ETH for its accounts")
            start_hub(i, next(c for c in chunks if c[0] == i)[1:])

        transfer_many(funder, list(lump.items()), gas, gas_price, stop_after, hub_paid)

    for t in threads:
        t.join()
    log(f"ETH: done, {sent['n']}/{len(todo)} funded")


def transfer_many(sender, items, gas, gas_price, stop_after, on_paid):
    """Send (index, value) transfers one by one from `sender`; call on_paid(index) after each receipt."""
    web3 = w3()
    nonce = None
    for i, value in items:
        if stop_after["stop"]:
            return
        to = Web3.to_checksum_address(_records[i]["address"])
        for attempt in range(5):
            try:
                if nonce is None:
                    nonce = web3.eth.get_transaction_count(sender.address, "pending")
                tx = {"chainId": CHAIN_ID, "nonce": nonce, "to": to, "value": value, "gas": gas,
                      "maxFeePerGas": gas_price, "maxPriorityFeePerGas": 0}
                h = web3.eth.send_raw_transaction(sender.sign_transaction(tx).raw_transaction)
                web3.eth.wait_for_transaction_receipt(h, timeout=180, poll_latency=0.5)
                nonce += 1
                on_paid(i)
                break
            except Exception as e:
                msg = str(e)
                if "insufficient funds" in msg:
                    log(f"ETH: sender {sender.address} is out of ETH, stopping its transfers")
                    return
                log(f"ETH: [{i + 1}] send error {msg[:120]}, retry {attempt + 1}/5")
                time.sleep(3 + attempt * 3)
                nonce = None  # re-read: the tx may have landed despite the error


def main():
    load()
    done = set(read_lines(DONE))
    log(f"Boost: {len(_records)} accounts, threads {THREADS}, "
        f"send_eth={SEND_ETH} login={LOGIN} faucet={FAUCET} send_tokens={SEND_TOKENS}, logged in before: {len(done)}")

    q, stats = queue.Queue(), {"ok": 0, "err": 0, "stop": False}
    if SEND_ETH:  # phase 1: gas for everyone first, login/faucet are pointless without it
        fund(stats)
    if not (LOGIN or FAUCET or SEND_TOKENS):
        return
    # phase 2: login + faucet
    workers = [threading.Thread(target=worker, args=(q, done, stats), daemon=True) for _ in range(THREADS)]
    for t in workers:
        t.start()
    for i in range(len(_records)):
        q.put(i)
    for _ in workers:
        q.put(None)
    for t in workers:
        t.join()
    log(f"Boost done: ok {stats['ok']}, errors {stats['err']}" + (" (stopped: Proxy.txt empty)" if stats["stop"] else ""))


if __name__ == "__main__":
    main()
