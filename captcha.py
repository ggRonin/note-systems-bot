"""reCAPTCHA v2 (invisible) via a captcha service: the LiteSpeed "Bot Verification" page of note.systems.
Used by register.py. The service is called directly (no proxy); only the solved token goes through the proxy."""
import time

import requests

import config

SERVICES = {  # name -> (API, task type); all three take the same createTask / getTaskResult protocol
    "capsolver": ("https://api.capsolver.com", "ReCaptchaV2TaskProxyLess"),
    "2captcha": ("https://api.2captcha.com", "RecaptchaV2TaskProxyless"),
    "anticaptcha": ("https://api.anti-captcha.com", "RecaptchaV2TaskProxyless"),
}
FATAL = ("KEY_DOES_NOT_EXIST", "ZERO_BALANCE", "WRONG_USER_KEY", "IP_BANNED", "KEY_DENIED",
         "ACCOUNT_SUSPENDED", "INVALID_TASK_TYPE", "NO_SLOT_AVAILABLE_BALANCE")
SOLVE_TIMEOUT = 180  # seconds


class Fatal(Exception):
    """The captcha service cannot be used at all (bad key, no balance): stop signing in for this run.
    Not a RuntimeError, so the per-account retries do not swallow it."""


def _post(method, body):
    if config.CAPTCHA_SERVICE not in SERVICES:
        raise Fatal(f"captcha service stopped: config.CAPTCHA_SERVICE must be one of {', '.join(SERVICES)}")
    if not config.CAPTCHA_KEY:
        raise Fatal(f"captcha service stopped: put your {config.CAPTCHA_SERVICE} API key into captcha_key.txt")
    url = SERVICES[config.CAPTCHA_SERVICE][0]
    for attempt in range(5):
        try:
            r = requests.post(f"{url}/{method}", json={"clientKey": config.CAPTCHA_KEY, **body}, timeout=30).json()
            break
        except (requests.RequestException, ValueError) as e:
            if attempt == 4:
                raise RuntimeError(f"captcha service unreachable: {e}")
            time.sleep(3 + attempt * 3)
    if r.get("errorId"):
        code = r.get("errorCode") or ""
        msg = f"captcha {code}: {r.get('errorDescription', '')}"
        if any(f in code for f in FATAL):
            raise Fatal(f"captcha service stopped: {msg}")
        raise RuntimeError(msg)
    return r


def solve_recaptcha(page_url, site_key):
    """The g-recaptcha-response token for an invisible reCAPTCHA v2 on page_url."""
    task = _post("createTask", {"task": {"type": SERVICES[config.CAPTCHA_SERVICE][1], "websiteURL": page_url,
                                         "websiteKey": site_key, "isInvisible": True}})["taskId"]
    deadline = time.time() + SOLVE_TIMEOUT
    while time.time() < deadline:
        time.sleep(4)
        r = _post("getTaskResult", {"taskId": task})
        if r.get("status") == "ready":
            return r["solution"]["gRecaptchaResponse"]
    raise RuntimeError(f"captcha not solved in {SOLVE_TIMEOUT}s")
