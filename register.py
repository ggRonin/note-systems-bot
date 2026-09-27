"""Note Systems community: sign-in (SIWE) and nick. Used by main.py.

accounts.txt: one account per line, "seed phrase,user:pass@host:port[,nick]"
Proxy.txt:    spare proxies, "user:pass@host:port"

A dead proxy is replaced by the next line of Proxy.txt; that line is removed from
Proxy.txt and written into accounts.txt so it is never reused.

Each account gets a random nick, stored as the third field. The site only allows
setting it after the account's first deposit into a note (canEditProfile), so the
nick is applied on the first run after that; until then the account stays pending.
"""
import random
import threading
import time
from pathlib import Path

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

Account.enable_unaudited_hdwallet_features()

BASE = Path(__file__).resolve().parent
ACCOUNTS = BASE / "accounts.txt"
PROXIES = BASE / "Proxy.txt"
DONE = BASE / "registered.txt"

NICK_A = ["silent", "lucky", "crypto", "dark", "golden", "swift", "lazy", "brave", "cosmic", "frosty", "wild",
          "neon", "iron", "sunny", "hidden", "rapid", "chill", "salty", "mellow", "atomic", "noble", "shadow",
          "pixel", "quiet", "bold", "stormy", "misty", "royal", "tiny", "mad", "red", "blue", "degen", "based"]
NICK_B = ["fox", "whale", "otter", "raven", "tiger", "panda", "wolf", "falcon", "bull", "bear", "hawk", "lynx",
          "ape", "shark", "owl", "moose", "koala", "viper", "badger", "mantis", "cobra", "yak", "heron", "bison",
          "trader", "miner", "hodler", "farmer", "ronin", "monk", "pilot", "nomad", "wizard", "sensei"]
API = "https://note.systems/community/api/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
API_HEADERS = {
    "Accept": "*/*",
    "Origin": "https://note.systems",
    "Referer": "https://note.systems/community/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
ATTEMPTS = 4           # sign-in attempts per account


class ProxyError(Exception):
    """The proxy (or the site through it) is unusable; switch to another proxy."""


class Blocked(Exception):
    """Rate limit / anti-bot response; back off and retry."""


# Shared by all worker threads: every read-modify-write of the txt files happens under
# FILE_LOCK, so two threads never take the same spare proxy or overwrite each other's edits.
FILE_LOCK = threading.RLock()
LOG_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def read_lines(path):
    with FILE_LOCK:
        if not path.exists():
            return []
        return [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def write_lines(path, lines):
    with FILE_LOCK:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def proxy_dict(proxy):
    url = proxy if "://" in proxy else "http://" + proxy
    return {"http": url, "https": url}


def check(r):
    """Classify a response: raise Blocked on anti-bot/rate limit, ProxyError on proxy-side failures."""
    if r.status_code in (429, 503) or r.status_code == 403 and "json" not in r.headers.get("content-type", ""):
        raise Blocked(f"HTTP {r.status_code}")
    if r.status_code in (407, 502, 504):
        raise ProxyError(f"HTTP {r.status_code}")
    if "captcha" in r.text[:3000].lower() or "challenge" in r.text[:3000].lower():
        raise Blocked("challenge page")
    return r


def json_of(r):
    check(r)
    try:
        return r.json()
    except ValueError:
        raise Blocked(f"non-JSON reply (HTTP {r.status_code}): {r.text[:80]!r}")


def proxy_ok(proxy):
    try:
        r = requests.get("https://note.systems/community/", proxies=proxy_dict(proxy), timeout=20,
                         headers={**BROWSER_HEADERS, **PAGE_HEADERS})
        return r.status_code == 200
    except requests.RequestException:
        return False


def next_spare_proxy():
    """Pop the first spare proxy from Proxy.txt (removing it from the file)."""
    with FILE_LOCK:
        spare = read_lines(PROXIES)
        if not spare:
            return None
        proxy, rest = spare[0], spare[1:]
        write_lines(PROXIES, rest)
        return proxy


def parse(line):
    """'seed,proxy[,nick]' -> [seed, proxy, nick]"""
    parts = [p.strip() for p in line.split(",")]
    return (parts + ["", ""])[:3]


def update_account(line_no, proxy=None, nick=None):
    with FILE_LOCK:
        lines = read_lines(ACCOUNTS)
        seed, old_proxy, old_nick = parse(lines[line_no - 1])
        proxy, nick = proxy or old_proxy, nick or old_nick
        nick = nick.replace(",", "")  # a comma would break the line format
        lines[line_no - 1] = ",".join(p for p in (seed, proxy, nick) if p)
        write_lines(ACCOUNTS, lines)


def set_account_proxy(line_no, proxy):
    update_account(line_no, proxy=proxy)


def random_nick(taken):
    while True:
        a, b = random.choice(NICK_A), random.choice(NICK_B)
        style = random.random()
        if style < 0.35:
            nick = f"{a}{b}{random.randint(1, 999)}"
        elif style < 0.6:
            nick = f"{a.capitalize()}{b.capitalize()}"
        elif style < 0.8:
            nick = f"{a}_{b}"
        else:
            nick = f"{b}{random.randint(10, 9999)}"
        if len(nick) <= 24 and nick.lower() not in taken:
            return nick


def ensure_nick(line_no):
    """Return the account's nick, generating and saving one if missing."""
    with FILE_LOCK:
        lines = read_lines(ACCOUNTS)
        nick = parse(lines[line_no - 1])[2]
        if not nick:
            nick = random_nick({parse(l)[2].lower() for l in lines})
            update_account(line_no, nick=nick)
        return nick


def replace_proxy(line_no, reason):
    log(f"#{line_no}: {reason}, taking a new proxy")
    proxy = next_spare_proxy()
    if proxy is None:
        raise RuntimeError("Proxy.txt is empty")
    set_account_proxy(line_no, proxy)
    return proxy


def working_proxy(line_no, proxy):
    while not proxy_ok(proxy):
        proxy = replace_proxy(line_no, f"proxy dead ({proxy.split('@')[-1]})")
    return proxy


def sign_in(acct, proxy, line_no, do_nick):
    """Sign in and, if do_nick, make sure the account has a nick. Returns (me, nick, nick_status)."""
    s = requests.Session()
    s.proxies = proxy_dict(proxy)
    s.headers.update(BROWSER_HEADERS)
    address = acct.address.lower()
    try:
        # Open the page first like a browser does: picks up the CDN cookie (hcdn).
        check(s.get("https://note.systems/community/", headers=PAGE_HEADERS, timeout=30))
        time.sleep(random.uniform(1.5, 4))

        n = json_of(s.get(API + "nonce.php", params={"address": address}, headers=API_HEADERS, timeout=30))
        message = n.get("message", "")
        if not message.startswith("note.systems wants you to sign in with your Ethereum account:"):
            raise RuntimeError(f"unexpected sign-in message: {n}")

        signature = acct.sign_message(encode_defunct(text=message)).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        time.sleep(random.uniform(1, 3))  # time a human takes to confirm in the wallet

        data = json_of(s.post(API + "login.php", headers=API_HEADERS, timeout=30,
                              json={"address": address, "message": message, "signature": signature}))
        if not data.get("ok"):
            raise RuntimeError(f"login failed: {data}")

        me = json_of(s.get(API + "me.php", headers=API_HEADERS, timeout=30))
        if not me.get("signedIn"):
            raise RuntimeError(f"session not established: {me}")

        profile = me.get("profile") or {}
        current = profile.get("name")
        if current:  # the site already has a nick: never replace it, just keep the file in sync
            if parse(read_lines(ACCOUNTS)[line_no - 1])[2] != current:
                update_account(line_no, nick=current)
            return me, current, "already set, skip"
        if not do_nick:
            return me, None, None
        nick = ensure_nick(line_no)
        if not profile.get("canEditProfile"):
            return me, nick, "pending (needs first deposit)"
        time.sleep(random.uniform(1.5, 4))
        r = json_of(s.post(API + "profile.php", headers=API_HEADERS, json={"name": nick}, timeout=30))
        if not r.get("ok"):
            raise RuntimeError(f"name rejected: {r}")
        return me, nick, "set now"
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout,
            requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
        raise ProxyError(type(e).__name__)
    except requests.exceptions.ReadTimeout:
        raise ProxyError("read timeout")


def register(line_no, acct, proxy, do_nick=False):
    proxy = working_proxy(line_no, proxy)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return sign_in(acct, proxy, line_no, do_nick)
        except ProxyError as e:
            proxy = working_proxy(line_no, replace_proxy(line_no, f"proxy failed mid-login ({e})"))
        except Blocked as e:
            wait = 20 * attempt + random.uniform(0, 10)
            log(f"#{line_no}: blocked ({e}), attempt {attempt}/{ATTEMPTS}, waiting {wait:.0f}s")
            time.sleep(wait)
            if attempt >= 2:  # repeated block on the same IP: move to a fresh one
                proxy = working_proxy(line_no, replace_proxy(line_no, "IP looks blocked"))
        except RuntimeError as e:
            log(f"#{line_no}: {e}, attempt {attempt}/{ATTEMPTS}")
            time.sleep(5)
    raise RuntimeError(f"gave up after {ATTEMPTS} attempts")


def mark(path, address):
    with FILE_LOCK, path.open("a", encoding="utf-8") as f:
        f.write(address + "\n")


def stats(me):
    m = me.get("metrics") or {}
    return (f"points {m.get('points', 0)}, rank {me.get('rank') or '-'}, notes {m.get('notesCount', 0)}, "
            f"deposited {m.get('deposited', 0)} USDG, streak {m.get('streak', 0)}")


def run_account(line_no, acct, do_nick):
    """Sign in (every run, to read fresh stats) and handle the nick. Returns True."""
    proxy = parse(read_lines(ACCOUNTS)[line_no - 1])[1]
    me, nick, nick_status = register(line_no, acct, proxy, do_nick)
    if acct.address not in set(read_lines(DONE)):
        mark(DONE, acct.address)
    msg = f"#{line_no} {acct.address}: signed in | {stats(me)}"
    if nick:
        msg += f" | nick '{nick}': {nick_status}"
    log(msg)
    return True
