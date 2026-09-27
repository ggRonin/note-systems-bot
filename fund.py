"""Send testnet ETH for gas from account #1 to every other account in accounts.txt.

Usage: python fund.py [amount_eth]      (default 0.01)

Wallets that already hold at least half of the amount are skipped, so the script
can be re-run safely. RPC is called directly: the proxy provider denies the RPC host,
and a signed transaction does not depend on the IP it is broadcast from.
"""
import sys
import time
from pathlib import Path

from eth_account import Account
from web3 import Web3

Account.enable_unaudited_hdwallet_features()

BASE = Path(__file__).resolve().parent
ACCOUNTS = BASE / "accounts.txt"
RPC = "https://rpc.testnet.chain.robinhood.com"
CHAIN_ID = 46630
EXPLORER = "https://explorer.testnet.chain.robinhood.com/tx/"


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def main():
    amount = Web3.to_wei(sys.argv[1] if len(sys.argv) > 1 else "0.01", "ether")
    w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 30}))
    assert w3.eth.chain_id == CHAIN_ID, "wrong chain"

    seeds = [l.split(",", 1)[0].strip()
             for l in ACCOUNTS.read_text(encoding="utf-8").splitlines() if l.strip()]
    sender = Account.from_mnemonic(seeds[0])
    targets = [(i, Account.from_mnemonic(s).address) for i, s in enumerate(seeds[1:], start=2)]

    todo = [(i, a) for i, a in targets if w3.eth.get_balance(a) < amount // 2]
    balance = w3.eth.get_balance(sender.address)
    log(f"sender {sender.address}: {w3.from_wei(balance, 'ether')} ETH, "
        f"{len(todo)} of {len(targets)} wallets need {w3.from_wei(amount, 'ether')} ETH")
    if not todo:
        return
    gas_price = w3.eth.gas_price * 2  # headroom over the current base fee
    # Arbitrum Orbit: the gas limit also pays for L1 data, so 21000 is not enough; estimate it.
    gas = int(w3.eth.estimate_gas({"from": sender.address, "to": todo[0][1], "value": amount}) * 1.3)
    need = len(todo) * (amount + gas * gas_price)
    if balance < need:
        sys.exit(f"not enough ETH: need {w3.from_wei(need, 'ether')}")

    nonce = w3.eth.get_transaction_count(sender.address, "pending")
    for i, addr in todo:
        tx = {
            "chainId": CHAIN_ID,
            "nonce": nonce,
            "to": addr,
            "value": amount,
            "gas": gas,
            "maxFeePerGas": gas_price,
            "maxPriorityFeePerGas": 0,
        }
        try:
            h = w3.eth.send_raw_transaction(sender.sign_transaction(tx).raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
            status = "ok" if receipt.status == 1 else "FAILED"
            log(f"#{i} {addr}: {status} {EXPLORER}{h.hex() if h.hex().startswith('0x') else '0x' + h.hex()}")
            nonce += 1
        except Exception as e:
            log(f"#{i} {addr}: ERROR {e}")
            if "gas" in str(e) or "fee" in str(e) or "funds" in str(e):
                break  # the same error would repeat for every wallet
            nonce = w3.eth.get_transaction_count(sender.address, "pending")

    log(f"done, sender left: {w3.from_wei(w3.eth.get_balance(sender.address), 'ether')} ETH")


if __name__ == "__main__":
    main()
