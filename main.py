"""Note Systems orchestrator: runs the modules enabled in config.py for every account.

Usage: python main.py [line]      (a line number runs only that account)
"""
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from eth_account import Account

import apps
import bonds
import config
import deposit
import faucet
import optimizer
import shield
import streak_guard
import register
from register import ACCOUNTS, log, parse, read_lines

Account.enable_unaudited_hdwallet_features()

_local = threading.local()
_stop = threading.Event()  # set when Proxy.txt runs out: the other threads finish their account and stop


def chain():
    """One RPC connection per worker thread."""
    if not hasattr(_local, "chain"):
        _local.chain = faucet.connect()
    return _local.chain


def process(line_no):
    try:
        run(line_no)
    except Exception as e:  # an exception inside a pool thread would otherwise vanish silently
        log(f"#{line_no}: ERROR {e}")


def run(line_no):
    if _stop.is_set():
        return
    acct = Account.from_mnemonic(parse(read_lines(ACCOUNTS)[line_no - 1])[0])
    acted = False
    if config.FAUCET:
        try:
            acted |= faucet.claim_all(*chain(), line_no, acct)
        except Exception as e:
            log(f"#{line_no} {acct.address}: faucet ERROR {e}")
    # before DEPOSIT/SHIELD/BONDS: a salvage re-entry gets the USDG first
    if config.STREAK_GUARD:
        try:
            acted |= streak_guard.guard(chain()[0], line_no, acct)
        except Exception as e:
            log(f"#{line_no} {acct.address}: streak guard ERROR {e}")
    if config.OPTIMIZER:
        try:
            acted |= optimizer.optimize(chain()[0], line_no, acct, config.DELAY_BETWEEN_TX)
        except Exception as e:
            log(f"#{line_no} {acct.address}: optimizer ERROR {str(e)[:200]}")
    if config.DEPOSIT and not config.OPTIMIZER:
        try:
            acted |= deposit.deposit_all(chain()[0], line_no, acct, config.DEPOSIT_COUNT,
                                         config.DEPOSIT_PCT, config.DELAY_BETWEEN_TX)
        except Exception as e:
            log(f"#{line_no} {acct.address}: deposit ERROR {e}")
    if config.SHIELD and not config.OPTIMIZER:
        try:
            acted |= shield.shield_all(chain()[0], line_no, acct, config.DELAY_BETWEEN_TX)
            acted |= shield.pair_all(chain()[0], line_no, acct, config.DELAY_BETWEEN_TX)
        except Exception as e:
            log(f"#{line_no} {acct.address}: shield ERROR {e}")
    if config.BONDS:
        try:
            acted |= bonds.bonds_all(chain()[0], line_no, acct, config.BOND_USDG, config.DELAY_BETWEEN_TX)
        except Exception as e:
            log(f"#{line_no} {acct.address}: bonds ERROR {e}")
    tx = config.DELAY_BETWEEN_TX
    for on, name, step in (
            (config.DESK, "desk", lambda: apps.desk(chain()[0], line_no, acct, config.DESK_PCT, tx)),
            (config.STAKE, "stake", lambda: apps.stake(chain()[0], line_no, acct, config.STAKE_PCT, tx)),
            (config.LOCK, "lock", lambda: apps.lock(chain()[0], line_no, acct, config.LOCK_PCT, config.LOCK_DAYS, tx)),
            (config.VOTE, "vote", lambda: apps.vote(chain()[0], line_no, acct)),
            (config.BUYBACK, "buyback", lambda: apps.buyback(chain()[0], line_no, acct, tx)),
            (config.OMNICHAIN, "omnichain", lambda: apps.omnichain(chain()[0], line_no, acct, config.OMNICHAIN_PCT, tx))):
        if on:
            try:
                acted |= step()
            except Exception as e:
                log(f"#{line_no} {acct.address}: {name} ERROR {str(e)[:200]}")
    if config.LOGIN or config.NICK:
        try:
            acted |= register.run_account(line_no, acct, config.NICK)
        except Exception as e:
            log(f"#{line_no} {acct.address}: login/nick ERROR {e}")
            if "Proxy.txt is empty" in str(e):
                _stop.set()
    if acted:
        time.sleep(random.uniform(*config.DELAY_BETWEEN_ACCOUNTS))


def main():
    lines = list(range(1, len(read_lines(ACCOUNTS)) + 1))
    if len(sys.argv) > 1:
        lines = [int(sys.argv[1])]

    steps = [n for n, on in (("faucet", config.FAUCET), ("streak guard", config.STREAK_GUARD),
                             ("optimizer", config.OPTIMIZER),
                             ("deposit", config.DEPOSIT and not config.OPTIMIZER),
                             ("shield", config.SHIELD and not config.OPTIMIZER), ("bonds", config.BONDS),
                             ("desk", config.DESK), ("stake", config.STAKE), ("lock", config.LOCK),
                             ("vote", config.VOTE), ("buyback", config.BUYBACK), ("omnichain", config.OMNICHAIN),
                             ("login", config.LOGIN), ("nick", config.NICK)) if on]
    threads = max(1, min(config.THREADS, len(lines)))
    log(f"accounts: {len(lines)}, threads: {threads}, modules: {', '.join(steps) or 'none'}")
    if not steps:
        return

    with ThreadPoolExecutor(max_workers=threads) as pool:
        for i, line_no in enumerate(lines):
            pool.submit(process, line_no)
            if i < threads - 1:
                time.sleep(random.uniform(1, 3))  # stagger the first wave so threads don't start in the same second
    if _stop.is_set():
        log("stopped: Proxy.txt is empty")
    log("done")


if __name__ == "__main__":
    main()
