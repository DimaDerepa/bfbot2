"""Regressions found by the code review of the Claude filter / one-alert-per-model changes."""
import time

import pytest

from bot import ai
from bot.config import watch_from_dict
from bot.engine import AI_FALLBACK_AFTER
from bot.specs import model_key
from bot.state import State
from tests.test_engine import FakeStore, FakeTG, engine_with, mk


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(ai, "parse_requests", lambda text: [])
    ai.load_usage(State.load(""), 0.0)
    ai.STATUS.update({"streak": 0})
    yield
    ai.load_usage(State.load(""), 1.0)
    ai.STATUS.update({"streak": 0})


def _judge_all(model="samsung galaxy s25"):
    def fake(state, w, titles, lookup=(), stats=None):
        return {t: {"match": True, "year": None, "ram_gb": None, "model": model, "unverified": ""}
                for t in [*titles, *lookup]}
    return fake


def test_gate_rejected_cheapest_listing_does_not_hide_the_real_one(claude, monkeypatch):
    monkeypatch.setattr(ai, "verdicts", _judge_all())
    monkeypatch.setattr(ai, "gate", lambda state, w, offers: {o.key: "resigilat" not in o.title for o in offers})
    FakeStore.blocked = False
    FakeStore.offers = []
    w = watch_from_dict({"name": "s25", "query": "samsung galaxy s25", "max_price": 6000, "ai": True})
    tg = FakeTG()
    e = engine_with([w], tg=tg, first_run_digest=10)
    e.run_cycle()
    tg.messages.clear()
    scam = mk("Telefon Samsung Galaxy S25 256GB resigilat", 1999, pid="scam")
    FakeStore.offers = [scam, mk("Telefon Samsung Galaxy S25 256GB Navy", 3299, pid="legit")]
    e.run_cycle()
    assert len(tg.messages) == 1 and "3 299" in tg.messages[0][1] and "resigilat" not in tg.messages[0][1]
    tg.messages.clear()
    e.run_cycle()                                     # nothing new: silence, and no empty "0 предложений" message
    assert tg.messages == []
    FakeStore.offers = [scam, mk("Telefon Samsung Galaxy S25 256GB Navy", 2500, pid="legit")]
    e.run_cycle()
    assert len(tg.messages) == 1 and "2 500" in tg.messages[0][1]


def test_cached_gate_rejection_survives_budget_exhaustion(claude, monkeypatch):
    monkeypatch.setattr(ai, "verdicts", _judge_all())
    monkeypatch.setattr(ai, "_json_call", lambda *a, **k: {"items": [{"i": 0, "ok": False, "why": "fake"}]})
    FakeStore.blocked = False
    FakeStore.offers = []
    w = watch_from_dict({"name": "s25", "query": "samsung galaxy s25", "max_price": 6000})
    tg = FakeTG()
    e = engine_with([w], tg=tg)
    e.run_cycle()
    FakeStore.offers = [mk("Telefon Samsung Galaxy S25 256GB", 2999, pid="a")]
    e.run_cycle()                       # the gate rejects it (and caches that)
    tg.messages.clear()
    ai.load_usage(e.state, 0.01)
    ai.USAGE["usd"] = 1.0               # budget used up: Claude is not asked, but its verdicts still count
    monkeypatch.setattr(ai, "_json_call", lambda *a, **k: pytest.fail("no API call over budget"))
    e.state.offers[next(iter(e.state.offers))]["m"] = True     # e.g. an old record
    e.run_cycle()
    assert tg.messages == []


def test_ai_watch_keeps_history_of_sold_out_listing(claude, monkeypatch):
    monkeypatch.setattr(ai, "verdicts", _judge_all())
    monkeypatch.setattr(ai, "gate", lambda state, w, offers: {})
    FakeStore.blocked = False
    FakeStore.offers = [mk("Telefon Samsung Galaxy S25 256GB", 2999, pid="a")]
    w = watch_from_dict({"name": "s25", "query": "samsung galaxy s25", "max_price": 6000, "ai": True})
    tg = FakeTG()
    e = engine_with([w], tg=tg)
    e.run_cycle()
    assert "2 999" in tg.messages[-1][1]
    FakeStore.offers = [mk("Telefon Samsung Galaxy S25 256GB", 2999, stock=False, pid="a")]
    e.run_cycle()
    tg.messages.clear()
    FakeStore.offers = [mk("Telefon Samsung Galaxy S25 256GB", 2999, pid="a")]
    e.run_cycle()
    assert len(tg.messages) == 1 and "снова" in tg.messages[0][1]   # before: silence (no record update)


def test_claude_outage_holds_then_falls_back_and_tells_the_user(claude, monkeypatch):
    def down(*a, **k):
        ai._failed("HTTP 400: Your credit balance is too low")
        return None

    monkeypatch.setattr(ai, "_json_call", down)
    FakeStore.blocked = False
    FakeStore.offers = [mk("Telefon Samsung Galaxy S25 256GB", 2999, pid="a")]
    w = watch_from_dict({"name": "s25", "query": "samsung galaxy s25", "max_price": 6000, "ai": True})
    tg = FakeTG()
    e = engine_with([w], tg=tg)
    for _ in range(AI_FALLBACK_AFTER):
        e.run_cycle()
        assert not any("s25" in t for _, t in tg.messages)     # no misleading "nothing found" digest
    assert e.state.data["ai_fail_runs"] == AI_FALLBACK_AFTER
    e.run_cycle()
    texts = "\n".join(t for _, t in tg.messages)
    assert "credit balance" in texts and "простые фильтры" in texts
    assert "2 999" in texts                               # rule-based checks took over


def test_peer_runner_cheaper_offer_suppresses_duplicate_alert(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    FakeStore.blocked = False
    FakeStore.offers = [mk("Consola Sony PlayStation 5 Slim Digital", 2200, pid="x")]
    w = watch_from_dict({"name": "ps5", "query": "playstation 5 slim digital", "max_price": 3000})
    tg = FakeTG()
    e = engine_with([w], tg=tg)
    e.run_cycle()
    mkey = next(iter(e.state.offers.values()))["mk"]
    e.state.data["peer_offers"] = {f"{w.id}|emag::1": {"t": "Consola Sony PlayStation 5 Slim Digital", "p": 1000,
                                                       "m": True, "ls": int(time.time()), "mk": mkey, "s": "emag",
                                                       "u": "https://emag.ro/1"}}
    FakeStore.offers.append(mk("Consola Sony PlayStation 5 Slim Digital alba", 1050, pid="y"))
    tg.messages.clear()
    e.run_cycle()
    assert tg.messages == []
    assert "peer_offers" not in e.state._dump()           # read-only copy, never written back


def test_model_key_keeps_plus_models_and_laptop_configs_apart():
    assert model_key("Samsung Galaxy S25+, 12GB, 256GB") != model_key("Samsung Galaxy S25, 12GB, 256GB")
    assert model_key("Samsung Galaxy S25+, 12GB, 256GB") == model_key("Samsung Galaxy S25 Plus 12GB 256GB Navy")
    assert model_key("Laptop Lenovo Legion 5 15IRH8 i7-13700H 16GB 1TB RTX 4060") != \
        model_key("Laptop Lenovo Legion 5 15IRH8 i5-13500H 16GB 512GB RTX 4050")
    assert model_key("Casti Sony WH-1000XM5") != model_key("Casti Sony WH-1000XM4")
    assert model_key("Televizor LG OLED55C46LA, 139 cm") == model_key("Televizor OLED LG 55C46LA Smart, negru")


def test_home_runner_keeps_claude_cache_and_budget():
    from bot.cli import HOME_KEYS

    for k in ("ai_cache", "ai_gate", "ai_usage", "model_np", "alert_refs"):
        assert k in HOME_KEYS


def test_budget_covers_both_runners(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    ai.load_usage(State.load(""), 1.0, other_usd=0.7)
    ai.USAGE["usd"] = 0.31
    assert not ai.enabled()
    st = State.load("")
    ai.save_usage(st)
    assert st.data["ai_usage"]["usd"] == 0.31 and "other" not in st.data["ai_usage"]
    ai.load_usage(State.load(""), 1.0)


def test_next_run_waits_only_for_the_rest_of_the_interval(monkeypatch):
    import datetime as dt

    from bot import statesync as ss

    started = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=400)

    class R:
        def __init__(self, code, data=None):
            self.status_code, self._d, self.text = code, data, ""

        def json(self):
            return self._d

    slept = []
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "5")
    monkeypatch.delenv("BOT_JOB_STARTED", raising=False)
    monkeypatch.setattr(ss.requests, "get", lambda url, **k: R(200, {"run_started_at": started.isoformat().replace("+00:00", "Z")}))
    monkeypatch.setattr(ss.requests, "request", lambda m, url, **k: R(204))
    monkeypatch.setattr(ss.time, "sleep", slept.append)
    assert ss.next_run("watch-home.yml", 480) == 0
    assert slept and 70 <= slept[0] <= 90


def test_multi_product_request_keeps_the_others_when_one_is_already_tracked(claude, monkeypatch):
    from bot.config import Config, Settings
    from bot.engine import Engine
    from bot.tg_commands import CommandProcessor
    from tests.test_tg_ui import TG, msg

    base = {"product_type": "", "category": "electronics", "max_price": 3000, "min_price": None, "currency": "RON",
            "min_discount_pct": None, "ram_min_gb": None, "storage_min_gb": None, "year_min": None,
            "brands_include": [], "brands_exclude": [], "exclude_words": [], "other_requirements": ""}
    monkeypatch.setattr(ai, "parse_requests", lambda text: [
        dict(base, search_queries=["placa video rtx 5070"], summary_ru="RTX 5070"),
        dict(base, search_queries=["monitor 27 oled"], summary_ru="Монитор OLED 27")])
    tg = TG()
    e = Engine(Config(Settings(), []), State.load(""), tg)
    cp = CommandProcessor(e)
    tg.updates = [msg(1, "/start"), msg(2, "rtx 5070 до 3000")]
    cp.poll()
    n = len(e.all_watches())
    e.state.data["watches"] = [d for d in e.state.data["watches"] if "monitor" not in d.get("query", "")]
    assert len(e.all_watches()) == n - 1
    tg.updates = [msg(3, "rtx 5070 и oled монитор 27 до 3000")]
    cp.poll()
    assert any("monitor" in w.query for w in e.all_watches()) and len(e.all_watches()) == n


def test_thumbs_down_under_home_runner_alert_works_in_the_cloud(monkeypatch):
    from bot.config import Config, Settings
    from bot.engine import Engine
    from bot.tg_commands import CommandProcessor
    from tests.test_tg_ui import TG, btn, msg

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tg = TG()
    e = Engine(Config(Settings(), []), State.load(""), tg)
    cp = CommandProcessor(e)
    tg.updates = [msg(1, "/start"), msg(2, "samsung galaxy s25 до 5000")]
    cp.poll()
    w = e.all_watches()[0]
    title = "Telefon mobil Samsung Galaxy S25 Edge, 12GB RAM, 256GB"
    e.state.data["peer_alert_refs"] = {"abc": {"w": w.id, "mk": None, "t": title, "ts": int(time.time())}}
    tg.updates = [btn(3, "ng:abc")]
    cp.poll()
    w = e.all_watches()[0]
    assert model_key(title) in w.exclude_models          # works with Claude off too
    assert any(m == "answerCallbackQuery" and "больше не покажу" in p.get("text", "") for m, p in tg.calls)


def test_cloud_checks_home_shops_when_the_home_pc_is_silent(monkeypatch):
    from bot import cli

    st = State.load("")
    st.data["telegram"]["chat_id"] = "42"
    monkeypatch.setenv("BOT_HOME_ENABLED", "true")
    cli._check_home_runner(st, {"last_run": int(time.time()) - 600})
    assert cli.os.environ["BOT_HOME_ENABLED"] == "true" and not st.data.get("notices")
    cli._check_home_runner(st, {})                       # never ran (Python missing on the PC, ...)
    assert cli.os.environ["BOT_HOME_ENABLED"] == "stale" and "ещё ни разу" in st.data["notices"][0]
    monkeypatch.setenv("BOT_HOME_ENABLED", "true")
    cli._check_home_runner(st, {"last_run": int(time.time()) - 5 * 3600})
    assert len(st.data["notices"]) == 1                  # once a day


def test_kick_restarts_a_stopped_home_chain_once(tmp_path, monkeypatch):
    import json as _json

    from bot import statesync as ss

    class R:
        def __init__(self, code, data=None):
            self.status_code, self._d, self.text = code, data, ""

        def json(self):
            return self._d

    calls = []
    active = {"n": 0}

    def fake(method, url, **k):
        calls.append((method, url))
        if method == "GET":
            return R(200, {"total_count": active["n"]})
        return R(204)

    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(ss.requests, "request", fake)
    f = tmp_path / "state-home.json"
    f.write_text(_json.dumps({"last_run": time.time() - 60}))
    assert ss.kick("watch-home.yml", str(f), 30) == 0 and calls == []          # fresh: nothing
    f.write_text("{}")                                                          # never ran
    assert ss.kick("watch-home.yml", str(f), 30) == 0 and calls[-1][0] == "POST"
    calls.clear()
    active["n"] = 1                                                             # one is already waiting for the PC
    assert ss.kick("watch-home.yml", str(f), 30) == 0 and all(m == "GET" for m, _ in calls)
