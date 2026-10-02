"""Claim Note Systems testnet faucets for every account in accounts.txt.

Used by main.py.

Per address every 24h: 1,000 mock USDG and 10 of each mock stock token
(faucet() on each token contract). Tokens still on cooldown are skipped, so the
script can be run daily. RPC is called directly (the proxy provider denies it).
"""
from pathlib import Path

from eth_account import Account
from web3 import Web3

from register import log

Account.enable_unaudited_hdwallet_features()

BASE = Path(__file__).resolve().parent
RPC = "https://rpc.testnet.chain.robinhood.com"
CHAIN_ID = 46630

# Final testnet build (pre-season, deployed 2026-10-01). The Season 0 tokens are no longer counted.
TOKENS = {
    "USDG": "0x8F9231B0F448bA9AD045348437458721676c23BC",
    "AAPL": "0x49a6d7470694FB1D9621cA4A5215704588A8A7BB",
    "AMZN": "0x9989F639CEBE120e3077D9401E112E1390F87278",
    "COIN": "0x5f107f870e634bbE77a45f9e7cE12B36Abe43d25",
    "HOOD": "0xD2011A3b80F5297Ca2C4a1b4569D240a0140dCC4",
    "META": "0xe3a8c9b60713a8259cAC6d0f7C68b673f456E565",
    "MSFT": "0x38d035444832a5AaaFd4d66E79857210eF9fBB49",
    "NVDA": "0x5358C97891fB27C0Cd0c000398D9213B9E1299b0",
    "TSLA": "0x761AbC9e6Fd8464337BB6A21D56f83b4Ad66cEBA",
}
ABI = [
    {"type": "function", "name": "faucet", "stateMutability": "nonpayable", "inputs": [], "outputs": []},
    {"type": "function", "name": "faucetCooldownRemaining", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "decimals", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
]
MIN_ETH = Web3.to_wei("0.0005", "ether")




def claim_all(w3, contracts, decimals, line_no, acct):
    if w3.eth.get_balance(acct.address) < MIN_ETH:
        log(f"#{line_no} {acct.address}: no ETH for gas, run fund.py first")
        return False
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    claimed, waiting = [], []
    for sym, c in contracts.items():
        left = c.functions.faucetCooldownRemaining(acct.address).call()
        if left:
            waiting.append(f"{sym} {left // 3600 + 1}h")
            continue
        try:
            # deposit.send re-sends with a fresh nonce when another sender from this wallet took it
            from deposit import send  # local: deposit imports this module
            send(w3, acct, c.functions.faucet(), nonce)
            nonce += 1
            claimed.append(sym)
        except Exception as e:
            log(f"#{line_no} {acct.address}: {sym} ERROR {e}")
            nonce = w3.eth.get_transaction_count(acct.address, "pending")
    usdg = contracts["USDG"].functions.balanceOf(acct.address).call() / 10 ** decimals["USDG"]
    log(f"#{line_no} {acct.address}: claimed [{', '.join(claimed) or '-'}]"
        + (f", cooldown [{', '.join(waiting)}]" if waiting else "") + f", USDG balance {usdg:,.0f}")
    return bool(claimed)


def connect():
    """Return (w3, contracts, decimals) for claim_all."""
    from rpc import make_w3  # rate-limited, two endpoints, retries on 429
    w3 = make_w3()
    assert w3.eth.chain_id == CHAIN_ID, "wrong chain"
    contracts = {s: w3.eth.contract(address=a, abi=ABI) for s, a in TOKENS.items()}
    decimals = {s: c.functions.decimals().call() for s, c in contracts.items()}
    return w3, contracts, decimals
