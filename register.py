"""Note Systems community: sign-in (SIWE) and nick. Used by main.py.

accounts.txt: one account per line, "seed phrase,user:pass@host:port[,nick]"
Proxy.txt:    spare proxies, "user:pass@host:port"

A dead proxy is replaced by the next line of Proxy.txt; that line is removed from
Proxy.txt and written into accounts.txt so it is never reused. The replaced proxy goes
to Proxy_dead.txt, so it can be brought back by hand.

When PROXY_DOWN_AFTER proxies fail in a row with no working one in between, the cause is
the provider or the network, not the proxies: the breaker trips, no more spare proxies are
taken in this run and sign-in stops with "proxies stopped: ...".

Each account gets a random nick, stored as the third field. The site only allows
setting it after the account's first deposit into a note (canEditProfile), so the
nick is applied on the first run after that; until then the account stays pending.
"""
import random
import re
import threading
import time
from pathlib import Path

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

import captcha

Account.enable_unaudited_hdwallet_features()

BASE = Path(__file__).resolve().parent
ACCOUNTS = BASE / "accounts.txt"
PROXIES = BASE / "Proxy.txt"
DEAD_PROXIES = BASE / "Proxy_dead.txt"
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
PROXY_DOWN_AFTER = 5   # proxies failing in a row (all threads together) = the provider or the network is down
PROXY_RECHECK = 5      # seconds before a failed proxy is checked once more
# errors after which main.py / Boost.py stop signing in for the rest of the run
STOP_ERRORS = ("Proxy.txt is empty", "proxies stopped", "captcha service stopped")

# LiteSpeed "Bot Verification": a 403 page with an invisible reCAPTCHA v2 instead of the reply. The solved token
# is posted to VERIFY_URL through the same proxy; the server then lets that IP through for a while (no cookie).
COMMUNITY = "https://note.systems/community/"
VERIFY_URL = "https://note.systems/.lsrecap/recaptcha?"
SITE_KEY_RE = re.compile(r"""['"]sitekey['"]\s*:\s*['"]([\w-]+)['"]""")


class ProxyError(Exception):
    """The proxy (or the site through it) is unusable; switch to another proxy."""


class Blocked(Exception):
    """Rate limit / anti-bot response; back off and retry."""


# Shared by all worker threads: every read-modify-write of the txt files happens under
# FILE_LOCK, so two threads never take the same spare proxy or overwrite each other's edits.
FILE_LOCK = threading.RLock()
LOG_LOCK = threading.Lock()
LOGS = BASE / "logs"  # one file per day, logs/2026-10-28.log; git-ignored (wallet addresses)


def log(msg):
    """Print a line and append it to today's log file (a failing write never stops the bot)."""
    with LOG_LOCK:
        stamp = time.strftime("[%H:%M:%S]")
        print(stamp, msg, flush=True)
        try:
            LOGS.mkdir(exist_ok=True)
            with (LOGS / time.strftime("%Y-%m-%d.log")).open("a", encoding="utf-8") as f:
                f.write(f"{stamp} {msg}\n")
        except OSError:
            pass


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


def verification_key(r):
    """The reCAPTCHA site key when r is the LiteSpeed bot verification page, else None."""
    if "lsrecaptcha-form" not in r.text[:5000]:
        return None
    m = SITE_KEY_RE.search(r.text)
    return m.group(1) if m else None


def pass_verification(s, site_key):
    """Solve the bot verification for the session's proxy IP."""
    token = captcha.solve_recaptcha(COMMUNITY, site_key)
    r = s.post(VERIFY_URL, data={"g-recaptcha-response": token}, timeout=30, allow_redirects=False,
               headers={**PAGE_HEADERS, "Origin": "https://note.systems", "Referer": COMMUNITY,
                        "Sec-Fetch-Site": "same-origin"})
    if r.status_code not in (200, 301, 302, 303):
        raise Blocked(f"bot verification not accepted (HTTP {r.status_code})")


def fetch(s, method, url, **kw):
    """One request to note.systems; the bot verification page is solved once and the request repeated.
    If the page comes back anyway, check() turns it into Blocked."""
    r = s.request(method, url, **kw)
    site_key = verification_key(r)
    if site_key:
        pass_verification(s, site_key)
        r = s.request(method, url, **kw)
    return r


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


def page_status(proxy):
    """None when note.systems/community opens through the proxy, else the reason."""
    try:
        r = requests.get(COMMUNITY, proxies=proxy_dict(proxy), timeout=20,
                         headers={**BROWSER_HEADERS, **PAGE_HEADERS})
        # the bot verification page means the site answered: the proxy works, sign_in solves the captcha
        return None if r.status_code == 200 or verification_key(r) else f"HTTP {r.status_code}"
    except requests.RequestException as e:
        return type(e).__name__


class _ProxyBreaker:
    """Counts proxy failures in a row across all threads. A working proxy resets the count; PROXY_DOWN_AFTER
    failures trip it for the rest of the run, so an outage cannot burn through Proxy.txt."""

    def __init__(self):
        self.lock = threading.Lock()
        self.failures = 0
        self.down = None  # the reason, once tripped

    def ok(self):
        with self.lock:
            self.failures = 0

    def failed(self, reason):
        with self.lock:
            if self.down:
                return
            self.failures += 1
            if self.failures < PROXY_DOWN_AFTER:
                return
            # tell "the site refuses" from "the provider is down" through a spare proxy, never directly from this
            # PC's IP; the spare is only read, it stays in Proxy.txt
            spare = read_lines(PROXIES)[:1]
            probe = page_status(spare[0]) if spare else None
            if not spare:
                cause = "cause unknown, Proxy.txt is empty"
            elif probe is None:
                cause = "a spare proxy opens the site, so the accounts' proxies themselves are bad"
            elif probe.startswith("HTTP"):
                cause = f"the site refuses (a spare proxy gets {probe} too)"
            else:
                cause = f"proxy provider or network down (a spare proxy fails too: {probe})"
            self.down = f"{self.failures} proxies failed in a row, last: {reason}; {cause}"
            log(f"!! proxies stopped: {self.down}. Spare proxies are kept; sign-in is skipped for this run")

    def check(self):
        if self.down:
            raise RuntimeError(f"proxies stopped: {self.down}")


BREAKER = _ProxyBreaker()


def proxy_ok(proxy):
    """None when the proxy works, else the reason (checked twice, so one hiccup does not discard it)."""
    why = page_status(proxy)
    if why:
        time.sleep(PROXY_RECHECK)
        why = page_status(proxy)
    if why:
        BREAKER.failed(why)
    else:
        BREAKER.ok()
    return why


def retire_proxy(proxy):
    """Keep a replaced proxy in Proxy_dead.txt: after an outage it can go back into Proxy.txt by hand."""
    if proxy:
        with FILE_LOCK, DEAD_PROXIES.open("a", encoding="utf-8") as f:
            f.write(proxy + "\n")


def next_spare_proxy():
    """Pop the first spare proxy from Proxy.txt (removing it from the file).
    Refuses (RuntimeError) once the breaker has tripped."""
    BREAKER.check()
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
    BREAKER.check()
    log(f"#{line_no}: {reason}, taking a new proxy")
    proxy = next_spare_proxy()
    if proxy is None:
        raise RuntimeError("Proxy.txt is empty")
    with FILE_LOCK:
        retire_proxy(parse(read_lines(ACCOUNTS)[line_no - 1])[1])
        set_account_proxy(line_no, proxy)
    return proxy


def working_proxy(line_no, proxy):
    while True:
        why = proxy_ok(proxy)
        if not why:
            return proxy
        proxy = replace_proxy(line_no, f"proxy dead ({proxy.split('@')[-1]}: {why})")


def sign_in(acct, proxy, line_no, do_nick):
    """Sign in and, if do_nick, make sure the account has a nick. Returns (me, nick, nick_status)."""
    s = requests.Session()
    s.proxies = proxy_dict(proxy)
    s.headers.update(BROWSER_HEADERS)
    address = acct.address.lower()
    try:
        # Open the page first like a browser does: picks up the CDN cookie (hcdn).
        # (and passes the bot verification, when the site shows it)
        check(fetch(s, "GET", COMMUNITY, headers=PAGE_HEADERS, timeout=30))
        time.sleep(random.uniform(1.5, 4))

        n = json_of(fetch(s, "GET", API + "nonce.php", params={"address": address}, headers=API_HEADERS, timeout=30))
        message = n.get("message", "")
        if not message.startswith("note.systems wants you to sign in with your Ethereum account:"):
            raise RuntimeError(f"unexpected sign-in message: {n}")

        signature = acct.sign_message(encode_defunct(text=message)).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        time.sleep(random.uniform(1, 3))  # time a human takes to confirm in the wallet

        data = json_of(fetch(s, "POST", API + "login.php", headers=API_HEADERS, timeout=30,
                             json={"address": address, "message": message, "signature": signature}))
        if not data.get("ok"):
            raise RuntimeError(f"login failed: {data}")

        me = json_of(fetch(s, "GET", API + "me.php", headers=API_HEADERS, timeout=30))
        if not me.get("signedIn"):
            raise RuntimeError(f"session not established: {me}")
        # me.php carries the frozen Season 0 card; the live pre-season standing comes from season.php
        try:
            me["pre"] = json_of(fetch(s, "GET", API + "season.php", params={"season": "pre", "view": "me"},
                                      headers=API_HEADERS, timeout=30)).get("standing") or {}
        except (Blocked, ProxyError, RuntimeError, requests.RequestException):  # 504: the site's stats backend is down
            me["pre"] = None

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
        r = json_of(fetch(s, "POST", API + "profile.php", headers=API_HEADERS, json={"name": nick}, timeout=30))
        if not r.get("ok"):
            raise RuntimeError(f"name rejected: {r}")
        return me, nick, "set now"
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout,
            requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
        raise ProxyError(type(e).__name__)
    except requests.exceptions.ReadTimeout:
        raise ProxyError("read timeout")


def register(line_no, acct, proxy, do_nick=False):
    BREAKER.check()  # tripped: fail at once instead of timing out on a dead provider
    proxy = working_proxy(line_no, proxy)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            result = sign_in(acct, proxy, line_no, do_nick)
            BREAKER.ok()
            return result
        except ProxyError as e:
            BREAKER.failed(str(e))
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
    """Pre-season standing (the live season); Season 0 is closed and frozen."""
    p = me.get("pre")
    if p is None:
        return "pre-season: not available"
    streams = ", ".join(f"{k} {v:,.0f}" for k, v in sorted((p.get("streams") or {}).items(), key=lambda kv: -kv[1]) if v)
    return (f"pre-season points {p.get('points', 0):,.2f}, rank {p.get('rank') or '-'}, apps {p.get('maxApps', 0)}, "
            f"quests {len(p.get('quests') or [])}" + (f" ({streams})" if streams else ""))


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
