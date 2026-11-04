"""HTTP layer: browser-like TLS fingerprint (curl_cffi), retries, per-host politeness, block detection."""
from __future__ import annotations

import codecs
import email.utils
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

log = logging.getLogger("bot.net")

try:  # curl_cffi imitates Chrome's TLS/HTTP2 fingerprint -> far fewer bot blocks
    from curl_cffi import requests as _cffi_requests  # type: ignore

    HAVE_CFFI = True
except Exception:  # pragma: no cover
    _cffi_requests = None
    HAVE_CFFI = False

import requests as _requests

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

DEFAULT_IMPERSONATE = "chrome"   # curl_cffi alias for its newest Chrome profile; BOT_IMPERSONATE overrides it

# Retry policy. A request's worst case (every attempt failing) stays well under a minute, so one store that
# hangs (Akamai tarpit, shop down) cannot eat the 25-minute job: a timeout / connection error is retried once,
# 429/5xx up to `retries` times, and all attempts plus pauses share one time budget (`max_time`).
NET_RETRIES = 1                 # retries after a timeout / connection error
MAX_RETRY_WAIT = 30.0           # longest pause before a 429/5xx retry (a longer Retry-After -> give up now)
MIN_ATTEMPT_TIME = 5.0          # no retry is started with less time than this left in the budget
DEFAULT_MAX_TIME = 45.0         # time budget of one request, all attempts and pauses included

BLOCK_STATUSES = (403, 429, 503, 511)

# Anti-bot detection: only markers that exist on a real challenge / block page.
# NOT the bare "challenge-platform", "cf-chl-" or "datadome": Cloudflare injects its passive bot-management
# loader (/cdn-cgi/challenge-platform/scripts/jsd/main.js, .../h/b/scripts/jsd/<hash>/main.js) into NORMAL pages,
# in-page Turnstile widgets carry cf-chl-widget-* inputs, and DataDome customers load the js.datadome.co tag on
# every page; Imperva likewise injects /_Incapsula_Resource?SWJIYLWA=... into normal pages.
_CF_CHALLENGE_RE = re.compile(
    r"_cf_chl_opt"                                         # Cloudflare managed / JS challenge config
    r"|/cdn-cgi/challenge-platform/h/[a-z]+/orchestrate/"  # Cloudflare challenge orchestrator script
    r"|cf-browser-verification",                           # legacy "I'm Under Attack" interstitial
    re.I,
)
CF_SCAN_LIMIT = 300_000         # the Cloudflare markers are only searched in the first 300 KB
SMALL_PAGE = 60_000             # body markers below are only trusted on pages smaller than this
BLOCK_MARKERS = (               # searched (lower-case) in the first 6 KB of a small page
    "captcha-delivery.com",                                # DataDome captcha / interstitial page
    "_incapsula_resource?cwudnsai=", "incapsula incident id",   # Imperva block page
    "px-captcha",                                          # PerimeterX / HUMAN
    "sec-if-cpt-container", "akamai-challenge", "/_sec/cp_challenge",   # Akamai challenge
    "are you a robot", "verify you are human",
)
_TITLE_PREFIXES = ("just a moment", "doar un moment")     # Cloudflare challenge title (en / ro)
_TITLE_MARKERS = (
    "attention required! | cloudflare",                    # Cloudflare block page (error 1020 ...)
    "access to this page has been denied",                 # PerimeterX block page
    "pardon our interruption",                             # Imperva / Distil block page
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)(?:</title|$)", re.I | re.S)
_AKAMAI_REF_RE = re.compile(r"reference(?:\s|&#32;|&nbsp;)*(?:#|&#35;)\s*\d", re.I)   # "Reference #18.5e3e..."


def _page_title(head: str) -> str:
    m = _TITLE_RE.search(head)
    return " ".join(m.group(1)[:300].split()).lower() if m else ""


class FetchError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


class BlockedError(FetchError):
    """The site answered with an anti-bot page / 403 / 429."""


@dataclass
class Response:
    url: str
    status: int
    text: str
    headers: dict

    def json(self) -> Any:
        return json.loads(self.text.lstrip("\ufeff"))     # some PHP APIs send a UTF-8 BOM


_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?\s*([\w.:-]+)""", re.I)
_META_CHARSET_RE = re.compile(rb"""<meta[^>]{0,200}?charset\s*=\s*["']?\s*([\w.:-]+)""", re.I)
_CHARSET_ALIASES = {"utf8mb4": "utf-8", "utf8": "utf-8", "none": ""}   # MySQL names seen in PHP shops' headers


def _codec(name: Optional[str]) -> Optional[str]:
    name = _CHARSET_ALIASES.get((name or "").strip().lower(), (name or "").strip().lower())
    if not name:
        return None
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def body_text(r: Any) -> str:
    """Body of a curl_cffi / requests response as text. Never raises on a wrong or unknown charset.

    Charset: Content-Type header -> <meta charset> -> UTF-8, or cp1250 (Romanian legacy pages) when the
    undeclared body is not valid UTF-8. A UTF-8 BOM is dropped.
    """
    content = getattr(r, "content", None)
    if not isinstance(content, (bytes, bytearray)):
        return (r.text or "").lstrip("\ufeff")
    if not content:
        return ""
    ctype = ""
    for k, v in dict(getattr(r, "headers", None) or {}).items():
        if str(k).lower() == "content-type":
            ctype = str(v)
    m = _CHARSET_RE.search(ctype)
    declared = _codec(m.group(1)) if m else None
    enc = declared
    if enc is None and "json" not in ctype.lower():
        mm = _META_CHARSET_RE.search(bytes(content[:4096]))
        enc = _codec(mm.group(1).decode("ascii", "ignore")) if mm else None
    content = bytes(content)
    if enc in (None, "utf-8"):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("utf-8" if enc else "cp1250", errors="replace")
    else:
        try:
            text = content.decode(enc, errors="replace")
        except LookupError:        # a non-text codec name ("rot13", "base64") in a broken header
            text = content.decode("utf-8", errors="replace")
    return text.lstrip("\ufeff")


class _HostThrottle:
    """Global per-host minimum delay between requests (shared by all threads)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, host: str, delay: float) -> None:
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next.get(host, 0.0))
            self._next[host] = at + delay * random.uniform(0.8, 1.3)
        sleep = at - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)


_THROTTLE = _HostThrottle()


def looks_blocked(status: int, text: str) -> bool:
    """True for an anti-bot answer: a block status (403/429/503/511) or a challenge / block page."""
    if status in BLOCK_STATUSES:
        return True
    if not text:
        return False
    head = text[:20000].lower()
    title = _page_title(head)
    if title:
        if title.startswith(_TITLE_PREFIXES) or any(m in title for m in _TITLE_MARKERS):
            return True
        if "access denied" in title and _AKAMAI_REF_RE.search(head):    # Akamai "Access Denied ... Reference #"
            return True
    if _CF_CHALLENGE_RE.search(text, 0, CF_SCAN_LIMIT):
        return True
    if len(text) < SMALL_PAGE:
        if "just a moment" in head[:2000]:
            return True
        if any(m in head[:6000] for m in BLOCK_MARKERS):
            return True
    return False


def _impersonate_targets() -> Optional[set]:
    """Names curl_cffi accepts for `impersonate` (None if this curl_cffi version can't tell)."""
    try:
        from curl_cffi.requests import BrowserType  # type: ignore

        names = {b.value for b in BrowserType}
    except Exception:  # pragma: no cover - very old / new curl_cffi
        return None
    try:
        from curl_cffi.requests.impersonate import REAL_TARGET_MAP  # type: ignore

        names |= set(REAL_TARGET_MAP)
    except Exception:  # pragma: no cover
        names |= {"chrome", "edge", "safari", "safari_ios", "chrome_android", "firefox"}
    return names


def resolve_impersonate(name: Optional[str] = None) -> str:
    """The TLS/HTTP2 profile to use: `name` (a store's pin) -> env BOT_IMPERSONATE -> "chrome".

    A name this curl_cffi does not know (a typo in BOT_IMPERSONATE) would fail every request, so it is
    replaced by the default with a warning.
    """
    chosen = (name or os.environ.get("BOT_IMPERSONATE", "") or DEFAULT_IMPERSONATE).strip() or DEFAULT_IMPERSONATE
    if HAVE_CFFI and chosen != DEFAULT_IMPERSONATE:
        known = _impersonate_targets()
        if known is not None and chosen not in known:
            log.warning("impersonate %r is not supported by this curl_cffi; using %r", chosen, DEFAULT_IMPERSONATE)
            return DEFAULT_IMPERSONATE
    return chosen


def _retry_after(headers: dict) -> Optional[float]:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date), None if absent or unreadable."""
    raw = None
    for k, v in (headers or {}).items():
        if str(k).lower() == "retry-after":
            raw = str(v).strip()
            break
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        import datetime as _dt

        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, dt.timestamp() - time.time())


class Http:
    """One instance per store (sessions are not thread-safe)."""

    def __init__(
        self,
        *,
        timeout: float = 25.0,
        delay: float = 1.5,
        retries: int = 2,
        proxy: Optional[str] = None,
        impersonate: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        browser_key: Optional[str] = None,
        backoff: float = 1.0,
        max_time: Optional[float] = None,
    ) -> None:
        self.timeout = float(timeout)
        self.delay = delay
        self.retries = max(0, int(retries))
        try:   # scales the pause before a 429/5xx retry (slow / bursty stores wait longer)
            self.backoff = max(0.0, float(backoff))
        except (TypeError, ValueError):
            self.backoff = 1.0
        # time budget of one request (all attempts and pauses); never shorter than one full attempt
        self.max_time = max(float(max_time) if max_time else DEFAULT_MAX_TIME, self.timeout)
        self.proxy = proxy
        self.headers = {**DEFAULT_HEADERS, **(extra_headers or {})}
        self.browser_key = browser_key   # store key -> enables headless-browser fallback if BOT_BROWSER allows it
        self.browser_used = 0
        self.requests_made = 0
        self.last_response: Optional[Response] = None   # kept even when an error is raised (diagnostics)
        self.impersonate = resolve_impersonate(impersonate)
        if HAVE_CFFI:
            self.session = _cffi_requests.Session(impersonate=self.impersonate)
            self._cffi = True
        else:  # pragma: no cover
            self.session = _requests.Session()
            self.headers.setdefault("User-Agent", UA)
            self._cffi = False
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def retry_wait(self, attempt: int, headers: Optional[dict] = None) -> float:
        """Pause before retrying a 429/5xx answer to attempt number `attempt` (0-based).

        min(30, 3 s * (attempt + 1) * backoff), but never shorter than the server's Retry-After.
        """
        wait = min(MAX_RETRY_WAIT, 3.0 * (attempt + 1) * self.backoff)
        ra = _retry_after(headers or {})
        if ra is not None:
            wait = max(wait, ra)
        return wait

    def _pause(self, seconds: float, started: float) -> bool:
        """Sleep before another attempt; False (no sleep) when the request's time budget can't fit one."""
        if seconds > MAX_RETRY_WAIT:
            return False
        if time.monotonic() - started + seconds + MIN_ATTEMPT_TIME > self.max_time:
            return False
        if seconds > 0:
            time.sleep(seconds)
        return True

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        data: Any = None,
        json_body: Any = None,
        allow_blocked: bool = False,
    ) -> Response:
        host = urlsplit(url).netloc
        hdrs = {**self.headers, **(headers or {})}
        last_exc: Optional[FetchError] = None
        net_failures = 0
        started: Optional[float] = None
        for attempt in range(self.retries + 1):
            _THROTTLE.wait(host, self.delay)
            now = time.monotonic()
            if started is None:
                started = now     # the budget starts with the first attempt (politeness wait excluded)
            remaining = self.max_time - (now - started)
            if attempt and remaining < MIN_ATTEMPT_TIME:
                break
            try:
                kw: dict[str, Any] = dict(params=params, headers=hdrs, timeout=min(self.timeout, remaining),
                                          allow_redirects=True)
                if data is not None:
                    kw["data"] = data
                if json_body is not None:
                    kw["json"] = json_body
                r = self.session.request(method, url, **kw)
            except Exception as e:  # network error / timeout
                last_exc = FetchError(f"{type(e).__name__}: {e}", None, url)
                net_failures += 1
                log.debug("fetch error %s (attempt %d): %s", url, attempt + 1, e)
                # a timeout / refused connection is retried once at most; no pause after the last attempt
                if net_failures > NET_RETRIES or attempt >= self.retries or not self._pause(2.0 * net_failures, started):
                    break
                continue
            self.requests_made += 1
            # decoded outside the network try: a bad charset is not a "timeout" (it would mark the store down)
            text = body_text(r)
            resp = Response(url=str(r.url), status=r.status_code, text=text, headers=dict(r.headers or {}))
            self.last_response = resp
            if allow_blocked:
                return resp
            if resp.status in (429, 500, 502, 503, 504) and attempt < self.retries:
                last_exc = FetchError(f"HTTP {resp.status}", resp.status, url)
                if self._pause(self.retry_wait(attempt, resp.headers), started):
                    continue
                break      # Retry-After too long / no time left: give up now instead of sleeping in vain
            if looks_blocked(resp.status, text):
                raise BlockedError(f"blocked (HTTP {resp.status})", resp.status, url)
            if resp.status >= 400:
                raise FetchError(f"HTTP {resp.status}", resp.status, url)
            return resp
        if last_exc is None:  # pragma: no cover - the loop always records why it stopped
            last_exc = FetchError("request time budget exhausted", None, url)
        if last_exc.status in (429, 503):
            raise BlockedError(str(last_exc), last_exc.status, url)
        raise last_exc

    def get(self, url: str, **kw: Any) -> Response:
        try:
            return self.request("GET", url, **kw)
        except BlockedError as blocked:
            if not self._browser_ok():
                raise
            return self._browser_get(url, kw.get("params"), blocked)

    def _browser_ok(self) -> bool:
        if not self.browser_key:
            return False
        from . import browser

        return browser.browser_enabled_for(self.browser_key) and browser.available()

    def _browser_get(self, url: str, params: Optional[dict], blocked: Optional[BlockedError] = None) -> Response:
        from urllib.parse import urlencode

        from . import browser

        full = url + (("&" if "?" in url else "?") + urlencode(params) if params else "")
        log.info("browser fallback for %s", full)
        try:
            resp = browser.fetch(self.browser_key or urlsplit(url).netloc, full, self.proxy)
        except Exception as e:  # browser missing / crashed / timed out: report the original block, not a crash
            log.warning("browser fallback failed for %s: %s: %s", full, type(e).__name__, e)
            status = blocked.status if blocked is not None else None
            raise BlockedError(f"blocked (HTTP {status}); browser fallback failed: {type(e).__name__}: {e}",
                               status, full) from e
        self.browser_used += 1
        self.requests_made += 1
        self.last_response = resp
        if looks_blocked(resp.status, resp.text):
            raise BlockedError(f"blocked even in browser (HTTP {resp.status})", resp.status, full)
        if resp.status >= 400:
            raise FetchError(f"HTTP {resp.status} (browser)", resp.status, full)
        return resp

    def get_json(self, url: str, **kw: Any) -> Any:
        headers = {"Accept": "application/json, text/plain, */*", **(kw.pop("headers", None) or {})}
        resp = self.get(url, headers=headers, **kw)
        try:
            return resp.json()
        except ValueError as e:
            if looks_blocked(resp.status, resp.text):
                raise BlockedError("blocked (non-JSON challenge page)", resp.status, url)
            raise FetchError(f"invalid JSON: {e}; body starts {resp.text[:120]!r}", resp.status, url)

    def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        headers = {"Accept": "application/json", "Content-Type": "application/json", **(kw.pop("headers", None) or {})}
        resp = self.request("POST", url, headers=headers, json_body=body, **kw)
        return resp.json()


def proxy_for(store_key: str) -> Optional[str]:
    """BOT_PROXY applies to all stores, or only to BOT_PROXY_STORES (comma list) if set."""
    proxy = os.environ.get("BOT_PROXY") or None
    if not proxy:
        return None
    only = [s.strip() for s in os.environ.get("BOT_PROXY_STORES", "").split(",") if s.strip()]
    if only and store_key not in only:
        return None
    return proxy
