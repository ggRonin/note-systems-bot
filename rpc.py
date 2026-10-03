"""Shared Robinhood testnet RPC access for every module.

- All threads share one request budget (RPS requests per second), so parallel work
  cannot trip the public RPC's 429 rate limit.
- Requests alternate between the official RPC and the dRPC mirror (both allowed by
  note.systems' own CSP).
- 429 / connection errors / timeouts are retried transparently with backoff, so a
  temporary limit never kills a worker thread. Re-sending the same signed tx is safe
  (same hash).
- Before a tx is sent, a stalled chain (no new block for STALL_AFTER s) is waited out.
"""
import itertools
import random
import threading
import time

import requests
from web3 import Web3
from web3.providers.rpc import HTTPProvider

URLS = ["https://rpc.testnet.chain.robinhood.com", "https://robinhood-testnet.drpc.org"]
RPS = 35          # total requests per second across all threads
RETRIES = 10
# JSON-RPC errors that mean "the node is busy", not "the request is wrong": retried like a 429
BUSY = ("context deadline exceeded", "timeout", "timed out", "rate limit", "too many requests", "temporarily unavailable",
        "temporary internal error", "please retry")
# The chain stalls now and then (once no block for 12 min): a tx sent meanwhile only fails. Before every send the
# latest block is checked; older than STALL_AFTER seconds = stalled, and the send waits (all threads together) until
# blocks move again, at most STALL_MAX_WAIT, so a merely quiet chain never blocks the bot for good.
STALL_AFTER = 90
STALL_MAX_WAIT = 15 * 60
STALL_POLL = 10


class _ChainWatch:
    def __init__(self):
        self.lock = threading.Lock()
        self.checked = 0.0  # last time the chain was seen moving

    def block_age(self, provider):
        block = provider.make_request("eth_getBlockByNumber", ["latest", False]).get("result") or {}
        return time.time() - int(block.get("timestamp", "0x0"), 16)

    def wait_live(self, providers):
        if time.time() - self.checked < 5:
            return
        with self.lock:  # one thread polls, the others wait on the lock
            if time.time() - self.checked < 5:
                return
            start, warned = time.time(), False
            while True:
                try:
                    age = min(self.block_age(p) for p in providers)
                except Exception:
                    age = 0  # cannot tell: let the send try and its own retries handle it
                if age <= STALL_AFTER or time.time() - start > STALL_MAX_WAIT:
                    if warned:
                        print(time.strftime("[%H:%M:%S]"), f"chain is moving again (waited {time.time() - start:.0f}s)",
                              flush=True)
                    self.checked = time.time()
                    return
                if not warned:
                    print(time.strftime("[%H:%M:%S]"), f"chain stalled: last block {age:.0f}s ago, sends wait",
                          flush=True)
                    warned = True
                time.sleep(STALL_POLL)


CHAIN = _ChainWatch()


class _Limiter:
    def __init__(self, rate):
        self.interval = 1.0 / rate
        self.next = time.monotonic()
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(self.next, now)
            self.next = slot + self.interval
        if slot > now:
            time.sleep(slot - now)


LIMITER = _Limiter(RPS)
_rr = itertools.count()


class RobustProvider(HTTPProvider):
    def __init__(self):
        super().__init__(URLS[0], request_kwargs={"timeout": 30})
        self._inner = [HTTPProvider(u, request_kwargs={"timeout": 30}) for u in URLS]

    def make_request(self, method, params):
        if method == "eth_sendRawTransaction":
            CHAIN.wait_live(self._inner)
        last = None
        for attempt in range(RETRIES):
            LIMITER.wait()
            provider = self._inner[next(_rr) % len(self._inner)]
            try:
                response = provider.make_request(method, params)
            except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last = e
                time.sleep(min(30, 0.5 * 2 ** attempt) + random.uniform(0, 0.5))
                continue
            error = response.get("error") if isinstance(response, dict) else None
            message = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
            if error and method == "eth_sendRawTransaction" and (
                    "already known" in message or attempt > 0 and "nonce too low" in message):
                # "nonce too low" on a retry: the first, timed-out send was mined
                # an earlier attempt timed out on our side but reached the node: the tx is in
                return {"jsonrpc": "2.0", "id": response.get("id"), "result": Web3.to_hex(Web3.keccak(hexstr=params[0]))}
            if error and any(m in message for m in BUSY):
                last = RuntimeError(error)
                time.sleep(min(30, 0.5 * 2 ** attempt) + random.uniform(0, 0.5))
                continue
            return response
        raise last


def make_w3():
    return Web3(RobustProvider())
