"""HTTP layer: browser-like TLS fingerprint (curl_cffi), retries, per-host politeness, block detection."""
from __future__ import annotations

import json
import logging
import os
import random
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

BLOCK_MARKERS = (
    "cf-chl-", "challenge-platform", "just a moment...", "attention required! | cloudflare",
    "access denied</title>", "<title>access denied", "px-captcha", "_incapsula_resource",
    "captcha-delivery.com", "datadome", "are you a robot", "verify you are human",
    "sec-if-cpt-container", "akamai-challenge", "/_sec/cp_challenge",
)


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
        return json.loads(self.text)


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
    if status in (403, 429, 503, 511):
        return True
    head = text[:6000].lower()
    if len(text) < 60000 and any(m in head for m in BLOCK_MARKERS):
        return True
    return False


class Http:
    """One instance per store (sessions are not thread-safe)."""

    def __init__(
        self,
        *,
        timeout: float = 25.0,
        delay: float = 1.5,
        retries: int = 2,
        proxy: Optional[str] = None,
        impersonate: str = "chrome",
        extra_headers: Optional[dict] = None,
    ) -> None:
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self.proxy = proxy
        self.headers = {**DEFAULT_HEADERS, **(extra_headers or {})}
        self.requests_made = 0
        self.last_response: Optional[Response] = None   # kept even when an error is raised (diagnostics)
        if HAVE_CFFI:
            self.session = _cffi_requests.Session(impersonate=impersonate)
            self._cffi = True
        else:  # pragma: no cover
            self.session = _requests.Session()
            self.headers.setdefault("User-Agent", UA)
            self._cffi = False
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

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
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            _THROTTLE.wait(host, self.delay)
            try:
                kw: dict[str, Any] = dict(params=params, headers=hdrs, timeout=self.timeout, allow_redirects=True)
                if data is not None:
                    kw["data"] = data
                if json_body is not None:
                    kw["json"] = json_body
                r = self.session.request(method, url, **kw)
                self.requests_made += 1
                text = r.text or ""
                resp = Response(url=str(r.url), status=r.status_code, text=text, headers=dict(r.headers))
                self.last_response = resp
            except Exception as e:  # network error / timeout
                last_exc = FetchError(f"{type(e).__name__}: {e}", None, url)
                log.debug("fetch error %s (attempt %d): %s", url, attempt + 1, e)
                time.sleep(2 * (attempt + 1))
                continue
            if allow_blocked:
                return resp
            if resp.status in (429, 500, 502, 503, 504) and attempt < self.retries:
                retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                wait = min(float(retry_after), 20) if retry_after and str(retry_after).isdigit() else 3 * (attempt + 1)
                time.sleep(wait)
                last_exc = FetchError(f"HTTP {resp.status}", resp.status, url)
                continue
            if looks_blocked(resp.status, text):
                raise BlockedError(f"blocked (HTTP {resp.status})", resp.status, url)
            if resp.status >= 400:
                raise FetchError(f"HTTP {resp.status}", resp.status, url)
            return resp
        assert last_exc is not None
        if isinstance(last_exc, FetchError) and last_exc.status in (429, 503):
            raise BlockedError(str(last_exc), last_exc.status, url)
        raise last_exc

    def get(self, url: str, **kw: Any) -> Response:
        return self.request("GET", url, **kw)

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
