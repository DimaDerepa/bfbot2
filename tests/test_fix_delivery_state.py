"""Regression tests for the delivery / state area: alerts are recorded only after Telegram accepted them,
state is never replaced by an empty one, store/link health messages, Telegram command safety, daemon pacing."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
import requests
import yaml

import bot.cli as cli
import bot.engine as engine_mod
import bot.notify as notify
from bot.config import Config, Settings, watch_from_dict
from bot.engine import Engine, http_for_store
from bot.models import Offer
from bot.net import FetchError
from bot.notify import Telegram, _eu_local_now, _strip_tags, in_quiet_hours
from bot.prices import fmt_price
from bot.state import State
from bot.stores.base import Store
from bot.tg_commands import HELP, CommandProcessor
from tests.test_engine import FakeStore, FakeTG, engine_with, mk, upd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_real_sleep = time.sleep   # the autouse fixture turns time.sleep into a no-op (Telegram back-off)


@pytest.fixture(autouse=True)
def _reset_fake_store(monkeypatch):
    FakeStore.blocked = False
    FakeStore.offers = []
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)   # Telegram.api back-off
    yield
    FakeStore.blocked = False
    FakeStore.offers = []


class FlakyTG(FakeTG):
    """FakeTG whose delivery can be switched off (Telegram outage)."""

    def __init__(self, chat_ids=("42",)):
        super().__init__(chat_ids)
        self.up = True
        self.attempts = 0

    def send(self, text, chat_id=None, preview=False, silent=False):
        self.attempts += 1
        if not self.up:
            return False
        return super().send(text, chat_id, preview, silent)


def texts(tg):
    return [t for _, t in tg.messages]


PS5 = {"name": "PS5", "query": "playstation 5 slim", "max_price": 2500}


# ====================================================================== Telegram.api / send (13)
class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class _Sess:
    """Scripted Telegram HTTP API: each item is (status, json) or an exception instance."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], json))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return _Resp(*item)


def _tg(script, chat_ids=("42",)):
    tg = Telegram("TOKEN", chat_ids)
    tg._s = _Sess(script)
    return tg


def test_api_retries_5xx_and_send_reports_delivery():
    ok = (200, {"ok": True, "result": {}})
    bad_gw = (502, {"ok": False, "error_code": 502, "description": "Bad Gateway"})
    tg = _tg([bad_gw, ok])
    assert tg.send("hello") is True
    assert len(tg._s.calls) == 2                      # 502 was retried

    tg = _tg([bad_gw])
    assert tg.send("hello") is False                  # Telegram down for good
    assert len(tg._s.calls) == 4

    tg = _tg([requests.ConnectionError("down")])
    assert tg.send("hello") is False

    tg = _tg([(400, {"ok": False, "description": "Bad Request: chat not found"})])
    assert tg.send("hello") is False

    assert Telegram(None, ["42"]).send("x") is False   # no token: nothing delivered
    assert Telegram("TOKEN", []).send("x") is False    # no chat


def test_send_counts_plain_text_fallback_and_any_target():
    parse_err = (400, {"ok": False, "description": "Bad Request: can't parse entities: Unsupported start tag"})
    ok = (200, {"ok": True})
    tg = _tg([parse_err, ok])
    assert tg.send("<b>x</b> /add rtx <3200") is True
    method, body = tg._s.calls[-1]
    assert "parse_mode" not in body and body["text"] == "x /add rtx <3200"   # literal "<3200" survives

    # a wrong extra id in TELEGRAM_CHAT_ID must not make every alert repeat for the good chat
    tg = _tg([(400, {"ok": False, "description": "chat not found"}), ok], chat_ids=("1", "42"))
    assert tg.send("deal") is True


# ====================================================================== engine: delivery-gated state (4, 13)
def test_alert_lost_during_telegram_outage_is_sent_later():
    FakeStore.offers = [mk("Consola Sony PlayStation 5 Slim", 2399, pid="a")]
    tg = FlakyTG()
    e = engine_with([watch_from_dict(PS5)], tg=tg)

    tg.up = False
    e.run_cycle()                                     # first-run digest cannot be delivered
    assert texts(tg) == [] and e.state.data["initialized"] == []
    tg.up = True
    e.run_cycle()
    assert len(tg.messages) == 1 and "найдено 1" in tg.messages[0][1]
    assert e.state.data["initialized"] == ["ps5"]

    tg.messages.clear()
    FakeStore.offers[0].price = 1999
    tg.up = False
    s = e.run_cycle()
    assert s["alerts"] == 0
    assert next(iter(e.state.offers.values()))["np"] == 2399   # not marked notified
    tg.up = True
    e.run_cycle()
    assert len(tg.messages) == 1 and "цена снизилась" in tg.messages[0][1] and fmt_price(1999) in tg.messages[0][1]
    tg.messages.clear()
    e.run_cycle()
    assert tg.messages == []                          # and only once


def test_rematch_alert_survives_failed_delivery():
    FakeStore.offers = [mk("Consola Sony PlayStation 5 Slim", 2399, pid="a")]
    tg = FlakyTG()
    e = engine_with([watch_from_dict(PS5)], tg=tg)
    e.run_cycle()
    FakeStore.offers[0].in_stock = False
    e.run_cycle()                                     # out of stock -> unmatched
    tg.messages.clear()
    FakeStore.offers[0].in_stock = True
    tg.up = False
    e.run_cycle()                                     # "снова подходит" cannot be delivered
    tg.up = True
    e.run_cycle()
    assert len(tg.messages) == 1 and "снова" in tg.messages[0][1]
    tg.messages.clear()
    e.run_cycle()
    assert tg.messages == []


def test_no_chat_bound_keeps_observing_and_sends_digest_after_start():
    FakeStore.offers = [mk("Consola Sony PlayStation 5 Slim", 1999, pid="a")]
    tg = FakeTG(chat_ids=())                          # TELEGRAM_CHAT_ID not set, nobody sent /start yet
    e = engine_with([watch_from_dict(PS5)], tg=tg)
    e.run_cycle()
    e.run_cycle()
    assert tg.messages == [] and e.state.data["initialized"] == []
    assert [r["p"] for r in e.state.best_offers(e.cfg.watches[0])] == [1999]   # history is still recorded

    cp = CommandProcessor(e)
    tg.updates = [upd(1, "/start")]
    cp.poll()
    tg.messages.clear()
    e.run_cycle()
    assert len(tg.messages) == 1 and tg.messages[0][0] == "42" and fmt_price(1999) in tg.messages[0][1]


def test_store_warning_is_retried_when_not_delivered():
    FakeStore.offers = [mk("Laptop ASUS TUF Gaming A15", 3999, pid="x")]
    FakeStore.blocked = True
    tg = FlakyTG()
    e = engine_with([watch_from_dict({"name": "tuf", "query": "asus tuf a15"})], tg=tg)
    tg.up = False
    e.run_cycle()
    e.run_cycle()                                     # threshold reached, but Telegram is down
    assert not e.state.data["stores"]["fake"].get("alerted")
    tg.up = True
    e.run_cycle()
    assert len(tg.messages) == 1 and "не отвечают" in tg.messages[0][1]


def test_quiet_hours_failure_never_blocks_sending(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no tz database")

    monkeypatch.setattr(engine_mod, "in_quiet_hours", boom)
    FakeStore.offers = [mk("Consola Sony PlayStation 5 Slim", 1999, pid="a")]
    tg = FakeTG()
    e = engine_with([watch_from_dict(PS5)], tg=tg)
    e.run_cycle()
    assert len(tg.messages) == 1 and e.state.data["initialized"] == ["ps5"]


# ====================================================================== drop_pct <= 0 (3)
@pytest.mark.parametrize("settings,spec", [({"notify_drop_pct": 0}, {}), ({}, {"drop_pct": -5})])
def test_zero_or_negative_drop_pct_does_not_repeat_alerts(settings, spec):
    FakeStore.offers = [mk("Consola Sony PlayStation 5 Slim", 2399, pid="a")]
    tg = FakeTG()
    e = engine_with([watch_from_dict({**PS5, **spec})], tg=tg, **settings)
    for _ in range(4):
        e.run_cycle()
    assert len(tg.messages) == 1                      # only the digest
    FakeStore.offers[0].price = 2398
    e.run_cycle()
    assert len(tg.messages) == 2 and "цена снизилась" in tg.messages[1][1]


# ====================================================================== store down / time budget (5)
class DownStore(Store):
    key = "down"
    name = "DownShop"
    base_url = "https://down.ro"
    domains = ("down.ro",)
    categories = ("general",)
    calls: list = []

    def search_page(self, query, page):
        DownStore.calls.append(query)
        raise FetchError("Timeout: Operation timed out", None, "https://down.ro/s")

    def product(self, url):
        DownStore.calls.append(url)
        raise FetchError("Timeout", None, url)


def test_store_marked_down_after_network_error_skips_rest():
    DownStore.calls = []
    e = engine_with([])
    run = e._run_store(DownStore, ["q1", "q2", "q3"], ["https://down.ro/p/1"])
    assert DownStore.calls == ["q1"] and run.down
    assert run.errors["q2"] == run.errors["q3"] == run.errors["https://down.ro/p/1"] == "skipped (store not responding)"
    assert "Timeout" in run.cause


class SlowStore(Store):
    key = "slow"
    name = "SlowShop"
    base_url = "https://slow.ro"
    categories = ("general",)
    calls: list = []

    def search_page(self, query, page):
        SlowStore.calls.append(query)
        _real_sleep(0.05)
        return []


def test_store_time_budget_skips_remaining_queries():
    SlowStore.calls = []
    e = engine_with([])
    e.store_budget = 0.03
    run = e._run_store(SlowStore, ["a", "b", "c"], [])
    assert SlowStore.calls == ["a"]
    assert run.skipped == {"b", "c"} and run.results == {"a": []}

    # a run where everything was skipped says nothing about the store's health
    e.store_budget = 0.0
    e.reg = {"slow": SlowStore}
    e.store_names = {"slow": "SlowShop"}
    e.cfg.watches = [watch_from_dict({"name": "x", "query": "abc"})]
    e.run_cycle()
    assert "slow" not in e.state.data["stores"]


# ====================================================================== bad / unreadable links (15, 16)
def test_malformed_url_does_not_stop_other_watches():
    FakeStore.offers = [mk("Consola Sony PlayStation 5 Slim", 1999, pid="a")]
    tg = FakeTG()
    e = engine_with([watch_from_dict(PS5)], tg=tg)
    cp = CommandProcessor(e)
    tg.updates = [upd(1, "/add https://[www.emag.ro/telefon/pd/D123 <2000")]
    cp.poll()
    tg.messages.clear()
    e.run_cycle()                                     # used to raise ValueError: Invalid IPv6 URL
    assert any("PS5" in t and fmt_price(1999) in t for t in texts(tg))
    e.run_cycle()
    warn = [t for t in texts(tg) if "не удаётся прочитать цену по ссылке" in t]
    assert len(warn) == 1 and "/del 2" in warn[0]


def test_unreadable_link_warns_once_and_reports_recovery(monkeypatch):
    result = {"offer": None}
    monkeypatch.setattr(FakeStore, "domains", ("fake.ro",), raising=False)
    monkeypatch.setattr(FakeStore, "product", lambda self, url: result["offer"])
    tg = FakeTG()
    w = watch_from_dict({"name": "exact", "urls": ["https://fake.ro/p/1"], "max_price": 1000})
    e = engine_with([w], tg=tg)
    for _ in range(5):
        e.run_cycle()
    warn = [t for t in texts(tg) if "не удаётся прочитать" in t]
    assert len(warn) == 1 and "цена на странице не найдена" in warn[0]
    result["offer"] = Offer(store="fake", title="Exact product", url="https://fake.ro/p/1", price=999, in_stock=True)
    tg.messages.clear()
    e.run_cycle()
    assert any("снова читается" in t for t in texts(tg))
    assert any("999" in t and "найдено 1" in t for t in texts(tg))
    tg.messages.clear()
    e.run_cycle()
    assert tg.messages == []


# ====================================================================== store-health flapping (17)
def test_store_health_rewarn_cooldown_and_recovery_only_after_warning():
    FakeStore.offers = [mk("Laptop ASUS TUF Gaming A15", 3999, pid="x")]
    tg = FakeTG()
    e = engine_with([watch_from_dict({"name": "tuf", "query": "asus tuf a15"})], tg=tg)

    def health_msgs():
        return [t for t in texts(tg) if "не отвечают" in t or "Снова работают" in t]

    def cycle(blocked):
        FakeStore.blocked = blocked
        e.run_cycle()

    cycle(True), cycle(True)
    cycle(False)
    assert len(health_msgs()) == 2                   # one warning, one recovery
    for _ in range(4):
        cycle(True)
    cycle(False)
    assert len(health_msgs()) == 2                   # flapped again within 24 h: silent both ways

    e.state.data["stores"]["fake"]["alerted_at"] -= 25 * 3600
    cycle(True), cycle(True)
    assert len(health_msgs()) == 3 and "не отвечают" in health_msgs()[-1]
    cycle(False)
    assert "Снова работают" in health_msgs()[-1]

    # a long outage that starts inside the cooldown is still reported once the cooldown is over
    for _ in range(3):
        cycle(True)
    assert len(health_msgs()) == 4
    e.state.data["stores"]["fake"]["alerted_at"] -= 25 * 3600
    cycle(True)
    assert len(health_msgs()) == 5 and "не отвечают" in health_msgs()[-1]
    cycle(True)
    assert len(health_msgs()) == 5

    # state written by the old code: alerted=True meant "warning sent" -> recovery is still announced
    e.state.data["stores"]["fake"] = {"ok": 1, "fail": 9, "cf": 9, "alerted": True}
    tg.messages.clear()
    cycle(False)
    assert any("Снова работают" in t for t in texts(tg))


# ====================================================================== /del + /add, collisions (18, 19)
def test_readd_after_del_gets_a_fresh_digest():
    FakeStore.offers = [mk("Consola Sony PlayStation 5 Slim", 2399, pid="a")]
    tg = FakeTG()
    e = engine_with([], tg=tg)
    cp = CommandProcessor(e)
    tg.updates = [upd(1, "/add playstation 5 slim до 2500")]
    cp.poll()
    e.run_cycle()
    tg.updates = [upd(2, "/del 1"), upd(3, "/add playstation 5 slim до 3000")]
    cp.poll()
    assert e.state.data["initialized"] == []
    tg.messages.clear()
    e.run_cycle()
    assert len(tg.messages) == 1 and "найдено 1" in tg.messages[0][1] and fmt_price(2399) in tg.messages[0][1]


def test_config_watch_with_telegram_watch_id_keeps_both():
    FakeStore.offers = [mk("Consola Sony PlayStation 5 Slim", 1900, pid="a")]
    st = State.load("")
    st.data["watches"] = [{"name": "playstation 5 slim", "query": "playstation 5 slim", "max_price": 2000.0,
                           "id": "playstation-5-slim"}]
    st.data["initialized"] = ["playstation-5-slim"]
    st.data["offers"] = {"playstation-5-slim|fake::a": {"t": "Consola Sony PlayStation 5 Slim", "u": "https://fake.ro/a",
                                                        "s": "fake", "p": 1900, "h": [[1, 1900]], "np": 1900,
                                                        "m": True, "ls": int(time.time()), "min": 1900}}
    cfgw = watch_from_dict({"name": "PlayStation 5 Slim", "query": "playstation 5 slim", "max_price": 2500})
    assert cfgw.id == "playstation-5-slim"
    tg = FakeTG()
    e = engine_with([cfgw], state=st, tg=tg)

    ws = e.all_watches()
    assert [(w.id, w.origin) for w in ws] == [("playstation-5-slim", "config"), ("playstation-5-slim-2", "telegram")]
    assert st.data["watches"][0]["id"] == "playstation-5-slim-2"
    assert st.data["initialized"] == ["playstation-5-slim-2"]
    assert list(st.offers) == ["playstation-5-slim-2|fake::a"]
    assert [w.id for w in e.all_watches()] == ["playstation-5-slim", "playstation-5-slim-2"]   # stable

    e.run_cycle()
    msgs = texts(tg)
    assert any("config.yaml" in t and "playstation-5-slim-2" in t for t in msgs)    # the user is told once
    digests = [t for t in msgs if "найдено" in t]
    assert len(digests) == 1 and "PlayStation 5 Slim" in digests[0]               # config watch: fresh digest
    tg.messages.clear()
    e.run_cycle()
    assert tg.messages == []


# ====================================================================== Telegram HTML validity (33)
ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "blockquote"}
_TAG_RE = re.compile(r"<(/?)([a-z][a-z0-9-]*)((?:\s+[a-z-]+=\"[^\"<>]*\")*)\s*>", re.I)
_ENT_RE = re.compile(r"&(?:lt|gt|amp|quot|#\d+|#x[0-9a-f]+);", re.I)


def assert_tg_html(text: str) -> None:
    """Telegram HTML parse mode: only supported tags, properly nested; every other '<' / '&' escaped."""
    stack: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "<":
            m = _TAG_RE.match(text, i)
            assert m, f"unescaped '<' at {i}: {text[max(0, i - 20):i + 30]!r}"
            closing, tag = m.group(1), m.group(2).lower()
            assert tag in ALLOWED_TAGS, f"unsupported tag <{tag}> in {text[:80]!r}"
            if closing:
                assert stack and stack[-1] == tag, f"unbalanced </{tag}> in {text[:80]!r}"
                stack.pop()
            else:
                assert tag == "a" or not m.group(3), f"attributes on <{tag}>"
                stack.append(tag)
            i = m.end()
        elif c == "&":
            assert _ENT_RE.match(text, i), f"unescaped '&' at {i}: {text[max(0, i - 20):i + 30]!r}"
            i += 1
        else:
            assert c != ">" or True
            i += 1
    assert not stack, f"unclosed tags {stack} in {text[:80]!r}"


def test_help_is_valid_telegram_html():
    assert_tg_html(HELP)
    assert "&lt;3200 &gt;2000" in HELP and "&lt;2000</code>" in HELP
    plain = _strip_tags(HELP)
    assert "/add rtx 5070 <3200 >2000 @pcgarage,emag,altex" in plain
    assert "/add https://www.emag.ro/... <2000" in plain


def test_all_reply_and_alert_templates_are_valid_telegram_html(monkeypatch):
    nasty = 'Consola <PlayStation> 5 Slim & "Pro"'
    FakeStore.offers = [Offer(store="fake", title=nasty + " a", url='https://fake.ro/a?x="1"&y=<2>', price=1999,
                              old_price=2899, in_stock=True, offer_id="a"),
                        Offer(store="fake", title=nasty + " b", url="https://fake.ro/b", price=2100,
                              in_stock=None, offer_id="b")]
    monkeypatch.setattr(FakeStore, "domains", ("fake.ro",), raising=False)
    monkeypatch.setattr(FakeStore, "product", lambda self, url: None)
    tg = FakeTG(chat_ids=())
    cfgw = watch_from_dict({"name": "PS <5> & co", "query": "playstation 5 slim", "max_price": 2500})
    e = engine_with([cfgw], tg=tg, max_alerts_per_watch=1)
    cp = CommandProcessor(e)
    tg.updates = [upd(1, "/start"), upd(2, "/help"), upd(3, "hello"),
                  upd(4, '/add tv <b>oled</b> & co <3200 >2000 @foo<bar> ^x&y -"a<b" +c>d ?e|f'),
                  upd(5, "/add https://fake.ro/p/<1> <2000"), upd(6, "/list"), upd(7, "/stores"), upd(8, "/status"),
                  upd(9, "/set 1 до 2400"), upd(10, "/pause 2"), upd(11, "/resume 2"), upd(12, "/check"),
                  upd(13, "/nosuch"), upd(14, "/top 9"), upd(15, "/del 9"), upd(16, "/add"), upd(17, "/set"),
                  upd(18, "/start", chat=666), upd(19, "/list", chat=666)]
    cp.poll()
    for _ in range(3):
        e.run_cycle()                                    # digest, link warning
    FakeStore.offers[0].price = 1500                     # drop alert + "…и ещё" line
    FakeStore.offers[1].price = 1400
    FakeStore.blocked = False
    e.run_cycle()
    FakeStore.blocked = True
    e.run_cycle(), e.run_cycle()                         # store warning
    FakeStore.blocked = False
    e.run_cycle()                                        # recovery
    tg.updates = [upd(20, "/top 1"), upd(21, "/stores"), upd(22, "/del 2"), upd(23, "/del 1")]
    cp.poll()
    kinds = " ".join(texts(tg))
    for needle in ("Чат привязан", "Добавлено", "Отслеживаю", "Магазины", "Последняя проверка", "Обновлено",
                   "Пауза", "Снова слежу", "Не знаю", "найдено", "не удаётся прочитать", "цена снизилась",
                   "не отвечают", "Снова работают", "самые дешёвые", "Удалено", "приватный"):
        assert needle in kinds, needle
    for _, t in tg.messages:
        assert_tg_html(t)


# ====================================================================== authorisation (34)
def test_telegram_chat_id_overrides_and_clears_a_stale_binding():
    tg = FakeTG(chat_ids=())
    e = engine_with([], tg=tg)
    tg.updates = [upd(1, "/start", chat=666)]          # a stranger was first
    CommandProcessor(e).poll()
    assert e.state.get("telegram", "chat_id") == "666"

    tg2 = FakeTG(chat_ids=("42",))                      # the owner sets TELEGRAM_CHAT_ID
    e2 = Engine(e.cfg, e.state, tg2)
    e2.reg, e2.store_names = e.reg, e.store_names
    cp = CommandProcessor(e2)
    tg2.updates = [upd(2, "/list", chat=666), upd(3, "/add spam spam", chat=666), upd(4, "/list", chat=42)]
    cp.poll()
    replies = dict(tg2.messages[:2])
    assert "приватный" in replies["666"]
    assert e.state.data["watches"] == []
    assert e.state.get("telegram", "chat_id") is None   # stale binding gone for good
    assert "приватный" not in tg2.messages[-1][1]
    assert cp.authorized("42") and not cp.authorized("666")


def test_poll_reports_failure():
    class DownTG(FakeTG):
        def api(self, method, _http_timeout=30, **params):
            return {"ok": False, "description": "Unauthorized"}

    e = engine_with([], tg=DownTG())
    assert CommandProcessor(e).poll() is None
    assert CommandProcessor(engine_with([], tg=Telegram(None))).poll() == 0


# ====================================================================== daemon pacing (35)
def test_backoff_helpers():
    assert [cli.poll_backoff(n) for n in range(0, 9)] == [0, 5, 10, 20, 40, 80, 160, 300, 300]
    assert [cli.cycle_backoff(n, 600) for n in (1, 2, 3, 4, 5, 9)] == [60, 120, 240, 480, 600, 600]


def _daemon(monkeypatch, tmp_path, tg, run_cycle):
    monkeypatch.setattr(cli.Telegram, "from_env", classmethod(lambda cls: tg))
    monkeypatch.setattr(cli.Engine, "run_cycle", run_cycle)
    monkeypatch.setattr(cli, "_print_summary", lambda s: None)
    monkeypatch.setattr(cli.traceback, "print_exc", lambda: None)
    saves = []
    orig = State.save

    def counting_save(self, path=None):
        saves.append(1)
        return orig(self, path)

    monkeypatch.setattr(State, "save", counting_save)
    sleeps: list[float] = []
    args = SimpleNamespace(config=str(tmp_path / "none.yaml"), state=str(tmp_path / "state.json"))
    return args, sleeps, saves


def test_daemon_backs_off_when_getupdates_fails(monkeypatch, tmp_path):
    class DeadTG(FakeTG):
        polls = 0

        def api(self, method, _http_timeout=30, **params):
            DeadTG.polls += 1
            return {"ok": False, "error_code": 401, "description": "Unauthorized"}

    cycles = []
    args, sleeps, saves = _daemon(monkeypatch, tmp_path, DeadTG(),
                                  lambda self: cycles.append(1) or {"watches": 0, "matches": 0, "alerts": 0,
                                                                   "stores": {}, "seconds": 0})
    cli.cmd_daemon(args, sleep=sleeps.append, max_loops=6)
    assert DeadTG.polls == 6 and len(cycles) == 1
    assert sleeps == [5, 10, 20, 40, 80, 160]          # no busy loop against api.telegram.org
    assert len(saves) <= 3                             # written when it changed, not on every pass


def test_daemon_does_not_rerun_a_crashing_cycle_right_away(monkeypatch, tmp_path):
    cycles = []

    def boom(self):
        cycles.append(1)
        raise ValueError("invalid literal for int() with base 10: 'eight'")

    args, sleeps, _ = _daemon(monkeypatch, tmp_path, Telegram(None), boom)
    cli.cmd_daemon(args, sleep=sleeps.append, max_loops=5)
    assert len(cycles) == 1                            # next try only after the back-off (>= 60 s)
    assert all(s >= 1 for s in sleeps) and len(sleeps) == 5


# ====================================================================== --dry-run (36)
def test_dry_run_does_not_touch_telegram(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:real")
    monkeypatch.setattr(Telegram, "api", lambda self, method, **kw: calls.append(method) or {"ok": True, "result": []})
    monkeypatch.setattr(Telegram, "send", lambda self, *a, **k: calls.append("send") or True)
    rc = cli.main(["--config", str(tmp_path / "none.yaml"), "run", "--dry-run", "--state", str(tmp_path / "s.json")])
    assert rc == 0 and calls == []


# ====================================================================== quiet hours without tz database (37)
def test_quiet_hours_without_tz_database(monkeypatch):
    import zoneinfo

    def missing(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", missing)
    assert in_quiet_hours("23-8") in (True, False)     # no exception
    assert in_quiet_hours("garbage") is False
    utc = dt.timezone.utc
    assert _eu_local_now(2, dt.datetime(2026, 1, 15, 22, 30, tzinfo=utc)).hour == 0          # winter: UTC+2
    assert _eu_local_now(2, dt.datetime(2026, 7, 15, 20, 30, tzinfo=utc)).hour == 23         # summer: UTC+3
    assert _eu_local_now(2, dt.datetime(2026, 3, 29, 0, 59, tzinfo=utc)).hour == 2            # DST starts 01:00 UTC
    assert _eu_local_now(2, dt.datetime(2026, 3, 29, 1, 0, tzinfo=utc)).hour == 4
    assert _eu_local_now(2, dt.datetime(2026, 10, 25, 0, 59, tzinfo=utc)).hour == 3           # DST ends 01:00 UTC
    assert _eu_local_now(2, dt.datetime(2026, 10, 25, 1, 0, tzinfo=utc)).hour == 3


def test_tzdata_in_requirements():
    reqs = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read().lower()
    assert re.search(r"^tzdata\b", reqs, re.M)


# ====================================================================== state file safety (1, 2)
def test_state_load_keeps_corrupt_file_and_refuses_unreadable(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{broken", encoding="utf-8")
    st = State.load(str(p))
    assert st.data["watches"] == []
    assert any(f.name.startswith("state.json.corrupt-") for f in tmp_path.iterdir())
    d = tmp_path / "dir.json"
    d.mkdir()
    with pytest.raises(OSError):                       # never start empty over a file we could not read
        State.load(str(d))


def test_save_if_changed(tmp_path):
    p = tmp_path / "s.json"
    st = State.load(str(p))
    assert st.save_if_changed() is True                # nothing on disk yet
    assert st.save_if_changed() is False
    st2 = State.load(str(p))
    assert st2.save_if_changed() is False
    st2.data["telegram"]["offset"] = 5
    assert st2.save_if_changed() is True and State.load(str(p)).get("telegram", "offset") == 5


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.mark.skipif(not (shutil.which("git") and shutil.which("bash")), reason="needs git and bash")
def test_state_scripts_never_overwrite_good_state(tmp_path):
    load = os.path.join(ROOT, "scripts", "load_state.sh")
    save = os.path.join(ROOT, "scripts", "save_state.sh")
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    env = {**os.environ, "STATE_REMOTE": str(origin), "STATE_LOAD_TRIES": "1", "GIT_TERMINAL_PROMPT": "0"}
    f = work / "state.json"

    def sh(script):
        return subprocess.run(["bash", script, "state.json"], cwd=work, env=env, capture_output=True, text=True)

    def branch_state():
        return json.loads(_git("show", "state:state.json", cwd=origin).stdout)

    r = sh(load)                                       # first run ever: no branch yet
    assert r.returncode == 0 and json.loads(f.read_text()) == {}
    good = {"version": 1, "telegram": {"offset": 120, "chat_id": "42"}, "watches": [{"id": "rtx-5070"}]}
    f.write_text(json.dumps(good))
    assert sh(save).returncode == 0 and branch_state() == good

    f.unlink()
    r = sh(load)
    assert r.returncode == 0 and json.loads(f.read_text()) == good

    env["STATE_REMOTE"] = str(tmp_path / "unreachable.git")   # transient failure
    r = sh(load)
    assert r.returncode == 1 and not f.exists() and (work / "state.json.load-failed").exists()
    env["STATE_REMOTE"] = str(origin)
    f.write_text("{}")                                 # even if something created a state file
    r = sh(save)
    assert r.returncode == 0 and branch_state() == good

    (work / "state.json.load-failed").unlink()
    f.write_text("{}")                                 # an empty state never replaces an existing branch
    assert sh(save).returncode == 0 and branch_state() == good

    seed = tmp_path / "seed"                           # a broken state.json on the branch is not "no state"
    seed.mkdir()
    _git("init", "-q", "-b", "state", cwd=seed)
    (seed / "state.json").write_text("{broken")
    _git("add", "state.json", cwd=seed)
    _git("-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qm", "x", cwd=seed)
    _git("push", "-qf", str(origin), "state:state", cwd=seed)
    r = sh(load)
    assert r.returncode == 1 and not f.exists()


def test_workflow_skips_save_when_load_failed():
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "watch.yml"), encoding="utf-8"))
    steps = {s.get("name"): s for s in wf["jobs"]["check"]["steps"]}
    assert steps["Load state"]["id"] == "load"
    assert "if" not in steps["Check prices"]           # default success(): skipped when the load failed
    assert "steps.load.outcome == 'success'" in steps["Save state"]["if"]


# ====================================================================== per-store Http options
def test_http_for_store_passes_impersonate_and_backoff_when_supported(monkeypatch):
    seen = {}

    class NewHttp:
        def __init__(self, *, timeout=25.0, delay=1.5, retries=2, proxy=None, impersonate="chrome",
                     extra_headers=None, browser_key=None, backoff=1.0):
            seen.update(locals())

    class OldHttp:
        def __init__(self, *, timeout=25.0, delay=1.5, retries=2, proxy=None, extra_headers=None, browser_key=None):
            seen.clear()
            seen.update(locals())

    class Pinned(Store):
        key = "pinned"
        name = "Pinned"
        base_url = "https://p.ro"
        min_delay = 3.0
        impersonate = "chrome124"

    monkeypatch.setattr(engine_mod, "Http", NewHttp)
    http_for_store(Pinned, Settings())
    assert seen["impersonate"] == "chrome124" and seen["backoff"] == 3.0 and seen["delay"] == 3.0
    monkeypatch.setattr(engine_mod, "Http", OldHttp)
    http_for_store(Pinned, Settings())                 # works before the net layer has these options
    assert "impersonate" not in seen and "backoff" not in seen
    monkeypatch.setattr(engine_mod, "Http", NewHttp)
    seen.clear()
    http_for_store(FakeStore, Settings())              # no attribute / no min_delay: Http defaults stay
    assert seen["impersonate"] == "chrome" and seen["backoff"] == 1.0


def test_engine_tolerates_non_numeric_settings():
    FakeStore.offers = [mk("Consola Sony PlayStation 5 Slim", 1999, pid="a")]
    tg = FakeTG()
    e = engine_with([watch_from_dict(PS5)], tg=tg, first_run_digest="eight", notify_drop_pct="x")
    e.run_cycle()
    assert len(tg.messages) == 1
