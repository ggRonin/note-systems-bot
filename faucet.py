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

TOKENS = {
    "USDG": "0x433947311e248C9fEc39c69Fac2a305661CCb907",
    "AAPL": "0x84f5E66636a75E2d65f4651e7B54682e6A572CAb",
    "AMZN": "0x189311a9B8E9e020b347813088822354fECcD900",
    "COIN": "0x05cAFA1C45f6175ecA86c96A30b9DB9686d14De7",
    "HOOD": "0x713ecbb623b879e5C6e51978c32b41dfe25de58D",
    "META": "0xD2B2cc2d8C66CD1E969D202b0F0F68eE2708fF9f",
    "MSFT": "0x6B6a2487f496cf7a79082Ae255D8d6EB235032CE",
    "NVDA": "0x238d2dF6750e47eCd93147bD2c1253cbf6E7aC1E",
    "TSLA": "0x8E2Ea3Bb2c548464f98b987E26dE5dfC65A0FEe7",
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
            tx = c.functions.faucet().build_transaction({
                "chainId": CHAIN_ID, "from": acct.address, "nonce": nonce,
                "maxFeePerGas": w3.eth.gas_price * 2, "maxPriorityFeePerGas": 0,
            })
            tx["gas"] = int(tx["gas"] * 1.3)
            h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
            if w3.eth.wait_for_transaction_receipt(h, timeout=120).status != 1:
                raise RuntimeError(f"reverted {h.hex()}")
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
