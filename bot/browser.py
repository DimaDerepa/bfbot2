"""Optional headless-browser fallback (Playwright) for sites that answer plain HTTP with a JS challenge.

Enabled with env BOT_BROWSER=1 (all stores) or BOT_BROWSER=pcgarage,flanco (only these).
Requires: pip install playwright && playwright install --with-deps chromium

Playwright's sync API is bound to one thread, so a single worker thread owns the browser and
store threads submit URLs to it through a queue (browser fetches are rare and sequential).
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Optional

log = logging.getLogger("bot.browser")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/136.0.0.0 Safari/537.36")
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ro-RO', 'ro', 'en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""


def browser_enabled_for(store_key: str) -> bool:
    v = os.environ.get("BOT_BROWSER", "").strip()
    if not v or v in ("0", "false", "no"):
        return False
    if v in ("1", "true", "yes", "all"):
        return True
    return store_key in {s.strip() for s in v.split(",")}


LAUNCH_RETRY_AFTER = 600.0   # after Chromium failed to start, don't try again for this long (s)
FETCH_TIMEOUT = 90.0         # longest a store thread waits for one browser fetch, queue time included (s)


def available() -> bool:
    """Playwright is installed and Chromium did not fail to start recently."""
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return _Worker.launch_failed() is None


class _Worker:
    _instance: Optional["_Worker"] = None
    _lock = threading.Lock()
    _launch_error: Optional[str] = None      # why Chromium could not start (e.g. `playwright install` not run)
    _launch_error_at = 0.0

    def __init__(self) -> None:
        self.q: "queue.Queue" = queue.Queue()
        self.dead = False                         # set (under qlock) when the worker thread gave up
        self.qlock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="browser", daemon=True)
        self.thread.start()

    @classmethod
    def launch_failed(cls) -> Optional[str]:
        """The recent Chromium start-up error, or None."""
        if cls._launch_error and time.monotonic() - cls._launch_error_at < LAUNCH_RETRY_AFTER:
            return cls._launch_error
        return None

    @classmethod
    def get(cls) -> "_Worker":
        with cls._lock:
            err = cls.launch_failed()
            if err:
                raise RuntimeError(f"headless browser unavailable: {err}")
            if cls._instance is None or not cls._instance.thread.is_alive():
                cls._instance = _Worker()
            return cls._instance

    def _fail_pending(self, exc: BaseException) -> None:
        """Fail every queued fetch at once (otherwise each caller would wait for its full timeout)."""
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                fut = item[3]
                if fut.set_running_or_notify_cancel():
                    fut.set_exception(exc)

    def _run(self) -> None:
        try:
            self._serve()
        except BaseException as e:  # Chromium missing / crashed: never leave callers waiting on a dead thread
            msg = f"{type(e).__name__}: {str(e).strip().splitlines()[0] if str(e).strip() else ''}"
            log.error("headless browser failed: %s", msg)
            with _Worker._lock:
                _Worker._launch_error = msg
                _Worker._launch_error_at = time.monotonic()
            with self.qlock:
                self.dead = True                  # fetch() refuses new items from now on ...
            self._fail_pending(RuntimeError(f"headless browser unavailable: {msg}"))   # ... and queued ones fail

    def _serve(self) -> None:
        from playwright.sync_api import sync_playwright

        from .net import Response, looks_blocked

        with sync_playwright() as p:
            launch_kw = dict(headless=True, args=["--disable-blink-features=AutomationControlled"])
            if os.environ.get("BOT_BROWSER_PATH"):
                launch_kw["executable_path"] = os.environ["BOT_BROWSER_PATH"]
            browser = p.chromium.launch(**launch_kw)
            contexts: dict = {}
            while True:
                item = self.q.get()
                if item is None:
                    break
                ctx_key, url, proxy, fut = item
                if not fut.set_running_or_notify_cancel():
                    continue      # the caller already gave up waiting: don't load the page for nobody
                page = None
                try:
                    ctx = contexts.get(ctx_key)
                    if ctx is None:
                        kw = dict(locale="ro-RO", user_agent=_UA, viewport={"width": 1366, "height": 900},
                                  timezone_id="Europe/Bucharest", extra_http_headers={"Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8"})
                        if proxy:
                            kw["proxy"] = {"server": proxy}
                        ctx = browser.new_context(**kw)
                        ctx.add_init_script(_STEALTH_JS)
                        contexts[ctx_key] = ctx
                    page = ctx.new_page()
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=40000)
                    ctype = (resp.headers.get("content-type", "") if resp else "")
                    if resp is not None and "json" in ctype:
                        fut.set_result(Response(resp.url, resp.status, resp.text(), dict(resp.headers)))
                        continue
                    html = page.content()
                    for _ in range(20):  # wait for a JS challenge to resolve itself
                        if not looks_blocked(200, html):
                            break
                        page.wait_for_timeout(1000)
                        html = page.content()
                    try:
                        page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass
                    html = page.content()
                    status = resp.status if resp is not None else 200
                    if status in (403, 429, 503) and not looks_blocked(200, html):
                        status = 200  # challenge passed after the initial 403
                    fut.set_result(Response(page.url, status, html, dict(resp.headers) if resp else {}))
                except Exception as e:
                    if not fut.done():
                        fut.set_exception(e)
                finally:
                    if page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass
            browser.close()

    def fetch(self, ctx_key: str, url: str, proxy: Optional[str], timeout: float = FETCH_TIMEOUT):
        fut: Future = Future()
        with self.qlock:
            if self.dead:
                raise RuntimeError(f"headless browser unavailable: {_Worker._launch_error or 'worker stopped'}")
            self.q.put((ctx_key, url, proxy, fut))
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout:
            fut.cancel()                    # still queued -> the worker skips it
            raise TimeoutError(f"browser fetch of {url} took longer than {timeout:.0f} s") from None


def fetch(ctx_key: str, url: str, proxy: Optional[str] = None):
    """Fetch `url` in headless Chromium; returns bot.net.Response."""
    return _Worker.get().fetch(ctx_key, url, proxy)
