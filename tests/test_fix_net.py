"""Regression tests for the net/registry area: retry budget (finding 5), anti-bot markers, impersonate/backoff,
browser fallback failing fast."""
from __future__ import annotations

import inspect
import socket
import threading
import time as _real_time
import types
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import pytest

from bot import browser, net
from bot.net import BlockedError, FetchError, Http, looks_blocked
from bot.stores.base import Store


# --------------------------------------------------------------------------- helpers

class Clock:
    """Virtual clock for bot.net: sleeps and request durations advance it instantly."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return _real_time.time() + (self.t - 1000.0)

    def sleep(self, s: float) -> None:
        self.sleeps.append(round(s, 3))
        self.t += max(0.0, s)


class _R:
    def __init__(self, status: int, text: str = "", headers: dict | None = None, url: str = "https://x.ro/") -> None:
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.url = url


class FakeSession:
    """Plays a script of answers: an int/tuple -> HTTP response, an Exception -> raised after `duration` s."""

    def __init__(self, clock: Clock, script: list, duration: float | None = None) -> None:
        self.clock = clock
        self.script = list(script)
        self.duration = duration
        self.calls: list[dict] = []
        self.proxies: dict = {}

    def request(self, method, url, **kw):
        self.calls.append(kw)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        # a hanging server costs the whole per-attempt timeout; a slow answer is cut off by it
        took = kw["timeout"] if isinstance(item, Exception) else (self.duration or 0.2)
        self.clock.t += min(took, kw["timeout"])
        if isinstance(item, Exception):
            raise item
        if took > kw["timeout"]:
            raise TimeoutError(f"Operation timed out after {kw['timeout']} s")
        status, text, headers = (item + ("", {}))[:3] if isinstance(item, tuple) else (item, "<html>ok</html>", {})
        return _R(status, text, headers, url)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(net, "time", types.SimpleNamespace(monotonic=c.monotonic, sleep=c.sleep, time=c.time))
    monkeypatch.setattr(net, "_THROTTLE", net._HostThrottle())    # per-host schedule on the virtual clock
    return c


def make_http(clock: Clock, script: list, duration: float | None = None, **kw) -> tuple[Http, FakeSession]:
    h = Http(delay=0, **kw)
    h.session = FakeSession(clock, script, duration)
    return h, h.session


# --------------------------------------------------------------------------- finding 5: retry budget

def test_timeout_is_retried_once_and_no_sleep_after_last_attempt(clock):
    h, s = make_http(clock, [TimeoutError("Operation timed out")], timeout=25.0, retries=2)
    t0 = clock.t
    with pytest.raises(FetchError) as ei:
        h.request("GET", "https://slow.ro/cauta?q=x")
    assert ei.value.status is None and not isinstance(ei.value, BlockedError)
    assert len(s.calls) == 2                      # 1 retry for a timeout, not `retries` (2)
    assert clock.sleeps == [2.0]                  # pause only between the attempts, none after the last one
    assert clock.t - t0 <= 45.0                   # was ~87 s (3 x 25 s + 2/4/6 s pauses)


def test_worst_case_request_time_stays_well_under_a_minute(clock):
    # hanging server, default settings
    h, _ = make_http(clock, [ConnectionError("timeout")])
    t0 = clock.t
    with pytest.raises(FetchError):
        h.request("GET", "https://slow.ro/")
    assert clock.t - t0 <= 46.0
    # slow 5xx answers (24 s each) with a slow store's backoff: the shared budget cuts the retries short
    h, s = make_http(clock, [502], duration=24.0, backoff=5.0)
    t0 = clock.t
    with pytest.raises(FetchError):
        h.request("GET", "https://slow.ro/")
    assert clock.t - t0 <= 46.0 and len(s.calls) <= 2
    # mixed: 503, then a hang, then a hang
    h, s = make_http(clock, [503, TimeoutError("t"), TimeoutError("t")], duration=20.0, backoff=3.0)
    t0 = clock.t
    with pytest.raises(FetchError):
        h.request("GET", "https://slow.ro/")
    assert clock.t - t0 <= 46.0


def test_retry_timeouts_never_exceed_the_remaining_budget(clock):
    h, s = make_http(clock, [TimeoutError("t")], timeout=25.0)
    with pytest.raises(FetchError):
        h.request("GET", "https://slow.ro/")
    assert s.calls[0]["timeout"] == 25.0 and s.calls[1]["timeout"] <= 45.0 - 25.0 - 2.0 + 1e-6


def test_timeout_then_success(clock):
    h, s = make_http(clock, [TimeoutError("t"), 200])
    assert h.request("GET", "https://x.ro/").status == 200
    assert len(s.calls) == 2 and clock.sleeps == [2.0]


def test_retries_zero_means_no_retry_and_no_sleep(clock):
    h, s = make_http(clock, [ConnectionError("refused")], retries=0)
    with pytest.raises(FetchError):
        h.request("GET", "https://x.ro/")
    assert len(s.calls) == 1 and clock.sleeps == []


def test_connection_refused_real_socket(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()                                  # nothing listens there -> connection refused
    slept: list[float] = []
    monkeypatch.setattr(net, "time", types.SimpleNamespace(monotonic=_real_time.monotonic, time=_real_time.time,
                                                           sleep=lambda s: slept.append(s)))
    h = Http(delay=0, timeout=3)
    with pytest.raises(FetchError) as ei:
        h.request("GET", f"http://127.0.0.1:{port}/")
    assert ei.value.status is None and slept == [2.0]


def test_tarpit_real_socket_is_bounded(monkeypatch):
    tar = socket.socket()
    tar.bind(("127.0.0.1", 0))
    tar.listen(10)
    conns = []
    threading.Thread(target=lambda: [conns.append(tar.accept()[0]) for _ in range(5)], daemon=True).start()
    monkeypatch.setattr(net, "time", types.SimpleNamespace(monotonic=_real_time.monotonic, time=_real_time.time,
                                                           sleep=lambda s: None))
    h = Http(delay=0, timeout=0.5, retries=2)
    t0 = _real_time.monotonic()
    with pytest.raises(FetchError) as ei:
        h.get(f"http://127.0.0.1:{tar.getsockname()[1]}/search")
    assert ei.value.status is None
    assert _real_time.monotonic() - t0 < 2.5      # 2 attempts of 0.5 s, not 3
    tar.close()


# --------------------------------------------------------------------------- (c) backoff / Retry-After

def test_429_backoff_scales_and_ends_in_blocked(clock):
    h, s = make_http(clock, [429], backoff=1.0)
    with pytest.raises(BlockedError) as ei:
        h.request("GET", "https://x.ro/")
    assert ei.value.status == 429 and len(s.calls) == 3 and clock.sleeps == [3.0, 6.0]

    clock.sleeps.clear()
    h, s = make_http(clock, [429], backoff=2.0)
    with pytest.raises(BlockedError):
        h.request("GET", "https://x.ro/")
    assert clock.sleeps == [6.0, 12.0]


def test_backoff_wait_is_capped_at_30_seconds(clock):
    h, _ = make_http(clock, [503], backoff=20.0)
    assert h.retry_wait(0) == 30.0 and h.retry_wait(5) == 30.0
    with pytest.raises(BlockedError):
        h.request("GET", "https://x.ro/")
    assert clock.sleeps == [30.0]                 # a second 30 s pause would not fit the 45 s budget


def test_retry_after_is_honoured(clock):
    h, s = make_http(clock, [(429, "", {"Retry-After": "7"}), 200])
    assert h.request("GET", "https://x.ro/").status == 200
    assert clock.sleeps == [7.0]
    # never shorter than our own backoff
    h, _ = make_http(clock, [(503, "", {"retry-after": "0"}), 200], backoff=1.0)
    clock.sleeps.clear()
    h.request("GET", "https://x.ro/")
    assert clock.sleeps == [3.0]


def test_retry_after_http_date(clock):
    when = datetime.now(timezone.utc) + timedelta(seconds=12)
    h, _ = make_http(clock, [(429, "", {"Retry-After": format_datetime(when, usegmt=True)}), 200])
    h.request("GET", "https://x.ro/")
    assert len(clock.sleeps) == 1 and 9.0 <= clock.sleeps[0] <= 13.0


def test_long_retry_after_gives_up_at_once(clock):
    h, s = make_http(clock, [(429, "", {"Retry-After": "3600"})])
    with pytest.raises(BlockedError):
        h.request("GET", "https://x.ro/")
    assert len(s.calls) == 1 and clock.sleeps == []


def test_http_signature_for_engine_wiring():
    params = inspect.signature(Http.__init__).parameters
    assert "impersonate" in params and params["impersonate"].default is None
    assert "backoff" in params and params["backoff"].default == 1.0
    assert Store.impersonate is None


def test_bad_backoff_value_falls_back_to_default():
    assert Http(backoff="abc").backoff == 1.0 and Http(backoff=-3).backoff == 0.0


# --------------------------------------------------------------------------- (b) impersonate

def test_impersonate_default_env_and_pin(monkeypatch):
    monkeypatch.delenv("BOT_IMPERSONATE", raising=False)
    assert Http().impersonate == "chrome"
    monkeypatch.setenv("BOT_IMPERSONATE", "chrome131")
    assert Http().impersonate == "chrome131"
    assert Http(impersonate="chrome124").impersonate == "chrome124"   # a store's pin beats the env


def test_unknown_impersonate_target_falls_back_to_chrome(monkeypatch, caplog):
    if not net.HAVE_CFFI:
        pytest.skip("curl_cffi not installed")
    monkeypatch.setenv("BOT_IMPERSONATE", "chrome999")
    with caplog.at_level("WARNING", logger="bot.net"):
        h = Http()
    assert h.impersonate == "chrome" and "chrome999" in caplog.text


# --------------------------------------------------------------------------- (a) anti-bot markers

CF_PASSIVE_LOADER = (
    "<script>(function(){function c(){var b=a.contentDocument||a.contentWindow.document;if(b){var d=b.createElement("
    "'script');d.innerHTML=\"window.__CF$cv$params={r:'8c1f2e3d4a5b6c7d',t:'MTcyNzI2MTQ1Ni4wMDAwMDA='};var a=document"
    ".createElement('script');a.nonce='';a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';document."
    "getElementsByTagName('head')[0].appendChild(a);\";b.getElementsByTagName('head')[0].appendChild(d)}}if(document."
    "body){var a=document.createElement('iframe');a.height=1;a.width=1;a.style.position='absolute';a.style.top=0;"
    "a.style.left=0;a.style.border='none';a.style.visibility='hidden';document.body.appendChild(a);if('loading'!=="
    "document.readyState)c();else if(window.addEventListener)document.addEventListener('DOMContentLoaded',c);}})();"
    "</script>"
)
CF_PASSIVE_LOADER_NEW = ("<script>(function(){var a=document.createElement('script');"
                         "a.src='/cdn-cgi/challenge-platform/h/b/scripts/jsd/5eaf848a0845/main.js';})();</script>")

NORMAL_CF_PAGE = (
    "<!DOCTYPE html><html lang='ro'><head><meta charset='utf-8'><title>Rezultate cautare: rtx 4070 - PC Garage</title>"
    "<link rel='preconnect' href='https://challenges.cloudflare.com'>"
    "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js' async defer></script></head><body>"
    "<div class='product_box'><a href='/placi-video/asus-rtx-4070/'>Placa video ASUS RTX 4070 12GB</a>"
    "<p class='price'>3.199,99 RON</p></div>"
    "<form class='newsletter'><div class='cf-turnstile' data-sitekey='0x4AAAAAAABkMYinukE8nzY'></div>"
    "<input type='hidden' name='cf-turnstile-response' id='cf-chl-widget-a1b2c_response'></form>"
    "<a href='/cdn-cgi/l/email-protection' class='__cf_email__' data-cfemail='1a7b'>[email&#160;protected]</a>"
    "<script src='/cdn-cgi/scripts/5c5dd728/cloudflare-static/email-decode.min.js'></script>"
    + CF_PASSIVE_LOADER + CF_PASSIVE_LOADER_NEW + "</body></html>"
)

CF_CHALLENGE_PAGE = (
    "<!DOCTYPE html><html lang=\"en-US\"><head><title>Just a moment...</title><meta http-equiv=\"Content-Type\" "
    "content=\"text/html; charset=UTF-8\"><meta name=\"robots\" content=\"noindex,nofollow\"><meta name=\"viewport\" "
    "content=\"width=device-width,initial-scale=1\"><style>*{box-sizing:border-box;margin:0;padding:0}html{line-height:"
    "1.15;color:#313131}</style><meta http-equiv=\"refresh\" content=\"390\"></head><body class=\"no-js\"><div "
    "class=\"main-wrapper\" role=\"main\"><div class=\"main-content\"><noscript><div class=\"h2\"><span id=\"challenge-"
    "error-text\">Enable JavaScript and cookies to continue</span></div></noscript></div></div><script>(function(){"
    "window._cf_chl_opt={cvId: '3',cZone: \"www.pcgarage.ro\",cType: 'managed',cRay: '8c1f2e3d4a5b6c7d',cH: 'x',"
    "cUPMDTk: \"\\/cauta\\/rtx?__cf_chl_tk=abc\",cFPWv: 'b'};var cpo = document.createElement('script');cpo.src = "
    "'/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=8c1f2e3d4a5b6c7d';document.getElementsByTagName("
    "'head')[0].appendChild(cpo);}());</script></body></html>"
)

DATADOME_NORMAL_PAGE = (
    "<html><head><title>Bormasina Bosch - Leroy Merlin</title><script>window.ddjskey = 'ABCDEF0123456789ABCDEF0123456"
    "789';window.ddoptions = {ajaxListenerPath: true, endpoint: 'https://api-js.datadome.co/js/'};</script>"
    "<script src='https://js.datadome.co/tags.js' async></script></head><body><h1>Bormasina Bosch</h1>"
    "<span class='price'>399 lei</span></body></html>"
)
DATADOME_CAPTCHA_PAGE = (
    "<html lang=\"en\"><head><title>leroymerlin.ro</title><style>#cmsg{animation: A 1.5s;}</style></head><body "
    "style=\"margin:0\"><p id=\"cmsg\">Please enable JS and disable any ad blocker</p><script data-cfasync=\"false\">"
    "var dd={'rt':'c','cid':'AHrlqAAAAAMAxyz','hsh':'A55FBF4311ED6F1BF9911EB71931D5','t':'fe','s':17434,'e':'abc',"
    "'host':'geo.captcha-delivery.com'}</script><script data-cfasync=\"false\" src=\"https://ct.captcha-delivery.com/"
    "c.js\"></script></body></html>"
)
AKAMAI_DENIED_PAGE = (
    "<HTML><HEAD>\n<TITLE>Access Denied</TITLE>\n</HEAD><BODY>\n<H1>Access Denied</H1>\n \nYou don't have permission "
    "to access \"http&#58;&#47;&#47;www&#46;sephora&#46;ro&#47;\" on this server.<P>\nReference&#32;&#35;18&#46;"
    "6f2c1302&#46;1727261456&#46;2b3c4d5e\n<P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18&#46;6f2c1302"
    "</P>\n</BODY>\n</HTML>\n"
)


def test_normal_cloudflare_page_with_passive_loader_is_not_blocked():
    assert len(NORMAL_CF_PAGE) < 6000 and "challenge-platform" in NORMAL_CF_PAGE and "cf-chl-" in NORMAL_CF_PAGE
    assert looks_blocked(200, NORMAL_CF_PAGE) is False
    # small "no results" page and a JSON body behind Cloudflare
    assert looks_blocked(200, "<html><title>Niciun rezultat</title><body>Nu am gasit produse."
                              + CF_PASSIVE_LOADER_NEW + "</body></html>") is False
    assert looks_blocked(200, '{"products": [], "script": "/cdn-cgi/challenge-platform/scripts/jsd/main.js"}') is False


def test_normal_pages_with_datadome_or_imperva_tags_are_not_blocked():
    assert "datadome" in DATADOME_NORMAL_PAGE and looks_blocked(200, DATADOME_NORMAL_PAGE) is False
    imperva = ("<html><head><title>Magazin</title><script src='/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd4'>"
               "</script></head><body>Produse</body></html>")
    assert looks_blocked(200, imperva) is False


def test_real_cloudflare_challenge_is_blocked():
    assert looks_blocked(200, CF_CHALLENGE_PAGE) and looks_blocked(403, CF_CHALLENGE_PAGE)
    # localized / unusual title and a big inline style that pushes the markers past the first 6 KB
    padded = CF_CHALLENGE_PAGE.replace("<title>Just a moment...</title>", "<title>www.pcgarage.ro</title>").replace(
        "</style>", "a{color:red}" * 1500 + "</style>")
    assert padded.find("_cf_chl_opt") > 6000 and looks_blocked(200, padded)
    assert looks_blocked(200, "<html><head><title>Doar un moment...</title></head><body></body></html>")
    assert looks_blocked(200, "<html><head><title>Attention Required! | Cloudflare</title></head></html>")


def test_datadome_akamai_and_status_blocks():
    assert looks_blocked(200, DATADOME_CAPTCHA_PAGE)
    assert looks_blocked(200, AKAMAI_DENIED_PAGE)
    assert looks_blocked(200, "<html><head><title>Access Denied</title></head><body>Reference #18.6f2c</body></html>")
    # a shop's own "access denied" page without Akamai's reference number is not an anti-bot block
    assert looks_blocked(200, "<html><head><title>Access denied</title></head><body>Login</body></html>") is False
    for status in (403, 429, 503):
        assert looks_blocked(status, "<html><title>Shop</title>ok</html>")
    assert looks_blocked(200, "") is False and looks_blocked(404, "not found") is False


def test_http_returns_normal_cloudflare_page_and_raises_on_challenge(clock):
    h, _ = make_http(clock, [(200, NORMAL_CF_PAGE)])
    assert h.get("https://www.pcgarage.ro/cauta/rtx").text == NORMAL_CF_PAGE
    h, _ = make_http(clock, [(200, CF_CHALLENGE_PAGE)])
    with pytest.raises(BlockedError):
        h.get("https://www.pcgarage.ro/cauta/rtx")


# --------------------------------------------------------------------------- browser fallback fails fast

@pytest.fixture
def fresh_worker(monkeypatch):
    monkeypatch.setattr(browser._Worker, "_instance", None)
    monkeypatch.setattr(browser._Worker, "_launch_error", None)
    monkeypatch.setattr(browser._Worker, "_launch_error_at", 0.0)
    yield
    browser._Worker._instance = None


def test_browser_launch_failure_fails_fast(monkeypatch, fresh_worker):
    def boom(self):
        raise RuntimeError("BrowserType.launch: Executable doesn't exist")

    monkeypatch.setattr(browser._Worker, "_serve", boom)
    t0 = _real_time.monotonic()
    for _ in range(3):
        with pytest.raises(RuntimeError, match="unavailable"):
            browser.fetch("pcgarage", "https://www.pcgarage.ro/")
    assert _real_time.monotonic() - t0 < 5        # was 120 s per fetch (dead worker thread)
    assert browser.available() is False


def test_blocked_request_with_broken_browser_reports_block(monkeypatch, fresh_worker, clock):
    monkeypatch.setenv("BOT_BROWSER", "1")
    monkeypatch.setattr(browser._Worker, "_launch_error", "Error: Executable doesn't exist")
    monkeypatch.setattr(browser._Worker, "_launch_error_at", _real_time.monotonic())
    h, _ = make_http(clock, [(403, "<title>Just a moment...</title>")], browser_key="pcgarage")
    with pytest.raises(BlockedError):              # the original block, browser skipped
        h.get("https://www.pcgarage.ro/")
    # a browser that crashes / times out mid-fetch also ends as BlockedError, not a "parser crash"
    monkeypatch.setattr(browser._Worker, "_launch_error", None)
    monkeypatch.setattr(browser, "available", lambda: True)

    def slow(*a, **k):
        raise TimeoutError("browser fetch took longer than 90 s")

    monkeypatch.setattr(browser, "fetch", slow)
    with pytest.raises(BlockedError, match="browser fallback failed"):
        h.get("https://www.pcgarage.ro/")


def test_browser_fetch_timeout_cancels_queued_item(monkeypatch, fresh_worker):
    gate = threading.Event()
    monkeypatch.setattr(browser._Worker, "_serve", lambda self: gate.wait(5))
    w = browser._Worker()
    with pytest.raises(TimeoutError):
        w.fetch("k", "https://x.ro/", None, timeout=0.2)
    item = w.q.get_nowait()
    assert item[3].cancelled()                    # the worker will skip it instead of loading it for nobody
    gate.set()


# --------------------------------------------------------------------------- body decoding never looks like a timeout

class _Raw:
    def __init__(self, content: bytes, ctype: str = "", status: int = 200) -> None:
        self.content = content
        self.headers = {"Content-Type": ctype} if ctype else {}
        self.status_code = status
        self.url = "https://x.ro/"

    @property
    def text(self) -> str:   # what curl_cffi does for charset=utf8mb4 + cp1250 bytes
        raise UnicodeDecodeError("utf-8", self.content, 0, 1, "invalid start byte")


def test_body_text_handles_bad_charsets_and_bom():
    page = "Frigider Samsung – preţ 2.499 lei, Maşină".encode("cp1250")
    assert "2.499 lei" in net.body_text(_Raw(page, "text/html; charset=utf8mb4"))
    assert net.body_text(_Raw(page, "text/html; charset=x-unknown-cs")).startswith("Frigider")
    meta = b"<html><head><meta charset='windows-1250'></head><body>" + "Maşină în stoc".encode("cp1250")
    assert "Maşină în stoc" in net.body_text(_Raw(meta, "text/html"))
    assert "preţ" in net.body_text(_Raw(page))                 # undeclared, not UTF-8 -> cp1250
    assert net.body_text(_Raw("ăîşţ".encode("utf-8"), "text/html")) == "ăîşţ"
    assert net.body_text(_Raw(b'\xef\xbb\xbf{"products": []}', "application/json; charset=utf-8")) == '{"products": []}'
    assert net.Response("u", 200, '﻿{"a": 1}', {}).json() == {"a": 1}


def test_undecodable_body_is_not_a_network_failure(clock):
    h = Http(delay=0)

    class S:
        calls = 0

        def request(self, method, url, **kw):
            S.calls += 1
            return _Raw("2.499 lei".encode("cp1250"), "text/html; charset=utf8mb4")

    h.session = S()
    assert "2.499 lei" in h.get("https://x.ro/").text and S.calls == 1
