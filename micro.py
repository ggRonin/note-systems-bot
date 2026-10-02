"""Micro COUPON deposits: many small USDG deposits per account, separate from main.py.

Usage: python micro.py [line] [-t THREADS] [-n COUNT]
  python micro.py              all accounts, THREADS and COUNT from the settings below
  python micro.py -t 20        20 accounts in parallel
  python micro.py 5 -n 100     only line 5 of accounts.txt, 100 deposits

Per account and run: COUNT depositCoupon calls of AMOUNT USDG (NoteCore minTicket is 10 USDG)
into random open series. Why USDG and not stock: a COUPON deposit is one call with no prefund,
uses ~115-133k gas (SHIELD ~280k) and 300 x 10 USDG is a small part of the balance.
One approve covers the whole run. Txs go out in bursts of BATCH consecutive nonces without
waiting for each receipt, then the burst's receipts are checked. The run stops early when the
account's ETH cannot pay for the next burst (run fund.py). Log: micro_v2.txt.
"""
import argparse
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eth_account import Account

from deposit import CORE, CORE_ABI, ERC20_EXTRA, is_nonce_error, open_series, sent_already
from faucet import ABI as ERC20_ABI, CHAIN_ID, TOKENS
from register import ACCOUNTS, FILE_LOCK, log, parse, read_lines
from rpc import make_w3

Account.enable_unaudited_hdwallet_features()

# ---------------------------------------------------------------- settings
COUNT = 300                  # deposits per account per run
AMOUNT = (10, 12)            # USDG per deposit (random in range, never below the series minTicket)
THREADS = 39                 # accounts in parallel (all share the RPC budget of rpc.py)
BATCH = 25                   # txs sent back to back before the receipts are checked
DELAY_BETWEEN_TX = (0.2, 0.8)
DELAY_BETWEEN_BATCHES = (2, 5)

BASE = Path(__file__).resolve().parent
MICRO = BASE / "micro_v2.txt"  # micro.txt is the Season 0 core
DEPOSIT_GAS = 200_000        # a depositCoupon uses ~115-133k gas; fixed limit saves an estimate per tx
APPROVE_GAS = 100_000


def human_amount(s):
    """AMOUNT in USDG (1e6), rounded like a person would type it: whole or 2 decimals."""
    x = random.uniform(*AMOUNT)
    x = round(x) if random.random() < 0.5 else round(x, 2)
    return max(int(x * 10**6), s["min"])


def send_raw(w3, acct, to, data, gas, nonce, fee):
    """Send without waiting. Returns (hash, nonce used). When the nonce was taken by another sender
    from this wallet (e.g. privacy pool relays), the tx goes out again with the next free nonce."""
    for attempt in range(5):
        tx = {"chainId": CHAIN_ID, "nonce": nonce, "to": to, "data": data, "value": 0,
              "gas": gas, "maxFeePerGas": fee, "maxPriorityFeePerGas": 0}
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
    return (h.hex() if h.hex().startswith("0x") else "0x" + h.hex()), nonce


def micro_all(w3, line_no, acct):
    usdg = w3.eth.contract(address=TOKENS["USDG"], abi=ERC20_ABI + ERC20_EXTRA)
    core = w3.eth.contract(address=CORE, abi=CORE_ABI)
    series = open_series(w3)
    if not series:
        log(f"#{line_no} {acct.address}: micro skip, no open series")
        return

    need = COUNT * int(AMOUNT[1] * 10**6)
    balance = usdg.functions.balanceOf(acct.address).call()
    count = min(COUNT, balance // max(int(AMOUNT[1] * 10**6), 1))
    if count < 1:
        log(f"#{line_no} {acct.address}: micro skip, USDG {balance / 1e6:,.2f}")
        return

    gas_price = w3.eth.gas_price
    fee = gas_price * 2
    per_tx = DEPOSIT_GAS * gas_price  # upper bound of the real cost (gasUsed < DEPOSIT_GAS)
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    if usdg.functions.allowance(acct.address, CORE).call() < need:
        data = usdg.encode_abi("approve", [CORE, need])
        h, nonce = send_raw(w3, acct, TOKENS["USDG"], data, APPROVE_GAS, nonce, fee)
        if w3.eth.wait_for_transaction_receipt(h, timeout=180).status != 1:
            log(f"#{line_no} {acct.address}: micro approve reverted {h}")
            return
        nonce += 1

    done, failed, spent, stop = 0, 0, 0, None
    while done < count and not stop:
        eth = w3.eth.get_balance(acct.address)
        room = int(eth // per_tx)
        size = min(BATCH, count - done, room)
        if size < 1:
            stop = f"no ETH for gas ({eth / 1e18:.6f} ETH left, run fund.py)"
            break
        sent = []
        for _ in range(size):
            open_now = [s for s in series if s["room"] >= int(AMOUNT[1] * 10**6)]
            if not open_now:
                stop = "all open series are full"
                break
            s = random.choice(open_now)
            amount = human_amount(s)
            data = core.encode_abi("depositCoupon", [s["id"], amount])
            try:
                h, nonce = send_raw(w3, acct, CORE, data, DEPOSIT_GAS, nonce, fee)
            except Exception as e:
                stop = f"send ERROR {str(e)[:150]}"
                nonce = w3.eth.get_transaction_count(acct.address, "pending")
                break
            nonce += 1
            s["room"] -= amount
            s["D"] += amount  # keep the shared series cache honest for main.py-style sizing
            sent.append((s, amount, h))
            time.sleep(random.uniform(*DELAY_BETWEEN_TX))
        if not sent:
            break

        # the last tx mined means every earlier nonce is mined too
        w3.eth.wait_for_transaction_receipt(sent[-1][2], timeout=300)
        lines = []
        for s, amount, h in sent:
            r = w3.eth.get_transaction_receipt(h)
            if r.status == 1:
                done += 1
                spent += amount
                lines.append(f"{acct.address},{s['id']},{amount / 1e6},{h}\n")
            else:
                failed += 1
                s["room"] += amount
                s["D"] -= amount
        with FILE_LOCK, MICRO.open("a", encoding="utf-8") as f:
            f.writelines(lines)
        log(f"#{line_no} {acct.address}: micro {done}/{count}" + (f", reverted {failed}" if failed else ""))
        if failed >= BATCH:
            stop = "too many reverts"
        time.sleep(random.uniform(*DELAY_BETWEEN_BATCHES))

    left = usdg.functions.balanceOf(acct.address).call() / 1e6
    log(f"#{line_no} {acct.address}: micro done {done}/{count}, {spent / 1e6:,.2f} USDG deposited, "
        f"USDG left {left:,.2f}" + (f" | stopped: {stop}" if stop else ""))


_local = threading.local()


def chain():
    """One RPC connection per worker thread."""
    if not hasattr(_local, "w3"):
        _local.w3 = make_w3()
    return _local.w3


def process(line_no):
    try:
        w3 = chain()
        acct = Account.from_mnemonic(parse(read_lines(ACCOUNTS)[line_no - 1])[0])
        micro_all(w3, line_no, acct)
    except Exception as e:  # an exception inside a pool thread would otherwise vanish silently
        log(f"#{line_no}: micro ERROR {str(e)[:200]}")


def main():
    global COUNT
    ap = argparse.ArgumentParser(description="Micro COUPON deposits for the accounts in accounts.txt")
    ap.add_argument("line", nargs="?", type=int, help="run only this line of accounts.txt")
    ap.add_argument("-t", "--threads", type=int, default=THREADS, help=f"accounts in parallel (default {THREADS})")
    ap.add_argument("-n", "--count", type=int, default=COUNT, help=f"deposits per account (default {COUNT})")
    args = ap.parse_args()
    COUNT = args.count

    lines = list(range(1, len(read_lines(ACCOUNTS)) + 1))
    if args.line:
        lines = [args.line]
    threads = max(1, min(args.threads, len(lines)))
    w3 = make_w3()
    assert w3.eth.chain_id == CHAIN_ID, "wrong chain"
    open_series(w3)  # fill the shared cache once before the threads start
    log(f"micro: accounts {len(lines)}, threads {threads}, {COUNT} x {AMOUNT[0]}-{AMOUNT[1]} USDG each")
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for i, line_no in enumerate(lines):
            pool.submit(process, line_no)
            if i < threads - 1:
                time.sleep(random.uniform(1, 3))
    log("done")


if __name__ == "__main__":
    main()
