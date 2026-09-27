"""Shared Robinhood testnet RPC access for every module.

- All threads share one request budget (RPS requests per second), so parallel work
  cannot trip the public RPC's 429 rate limit.
- Requests alternate between the official RPC and the dRPC mirror (both allowed by
  note.systems' own CSP).
- 429 / connection errors / timeouts are retried transparently with backoff, so a
  temporary limit never kills a worker thread. Re-sending the same signed tx is safe
  (same hash).
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
        last = None
        for attempt in range(RETRIES):
            LIMITER.wait()
            provider = self._inner[next(_rr) % len(self._inner)]
            try:
                return provider.make_request(method, params)
            except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last = e
                time.sleep(min(30, 0.5 * 2 ** attempt) + random.uniform(0, 0.5))
        raise last


def make_w3():
    return Web3(RobustProvider())
