from bot.config import Config, Settings
from bot.engine import Engine
from bot.models import Offer
from bot.state import State
from bot.stores.base import Store
from bot.tg_commands import CommandProcessor
from tests.test_tg_ui import TG, btn, msg


class Shop(Store):
    key, name, base_url, categories = "shop", "Shop", "https://shop.ro", ("general",)
    items: list = []

    def search_page(self, q, page):
        return list(Shop.items) if page == 1 else []


def sent(tg, needle=""):
    return [p for m, p in tg.calls if m == "send" and needle in p.get("text", "")]


def test_thumbs_down_hides_model_and_teaches_claude():
    Shop.items = [Offer(store="shop", title="Consola Sony PlayStation 5 Slim Digital", url="https://shop.ro/1", price=1999, offer_id="1"),
                  Offer(store="shop", title="Consola Sony PlayStation 5 Slim Disc", url="https://shop.ro/2", price=2299, offer_id="2")]
    tg = TG()
    e = Engine(Config(Settings(), []), State.load(""), tg)
    e.reg, e.store_names = {"shop": Shop}, {"shop": "Shop"}
    cp = CommandProcessor(e)
    tg.updates = [msg(1, "/start"), msg(2, "playstation 5 slim до 2500")]
    cp.poll()
    e.run_cycle()
    assert sent(tg, "найдено")
    refs = e.state.data["alert_refs"]
    tok = next(t for t, r in refs.items() if "Digital" in r["t"])
    tg.updates = [btn(3, f"ng:{tok}")]
    cp.poll()
    w = e.all_watches()[0]
    assert w.exclude_models and "Digital" in w.neg_examples[0]
    assert any(m == "answerCallbackQuery" and "больше не покажу" in p.get("text", "") for m, p in tg.calls)
    Shop.items[0].price = 1500        # even a big drop of the rejected model stays silent
    tg.calls.clear()
    e.run_cycle()
    assert not any("Digital" in p.get("text", "") for p in sent(tg))
    # "⏸ хватит" pauses the watch
    import hashlib

    wh = hashlib.sha1(w.id.encode()).hexdigest()[:8]
    tg.updates = [btn(4, f"wp:{wh}")]
    cp.poll()
    assert e.all_watches()[0].enabled is False


def test_claude_daily_budget_stops_calls(monkeypatch):
    from bot import ai

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    st = State.load("")
    ai.load_usage(st, 0.5)
    assert ai.enabled()
    ai.USAGE["usd"] = 0.6
    assert not ai.enabled()                # over budget -> rule-based filters take over
    ai.save_usage(st)
    assert st.data["ai_usage"]["usd"] == 0.6
    ai.load_usage(st, 0.0)                 # 0 = no limit
    assert ai.enabled()
    ai.load_usage(State.load(""), 1.0)     # reset for other tests


def test_irrelevant_titles_never_reach_claude(monkeypatch):
    from bot import ai

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    ai.load_usage(State.load(""), 1.0)
    judged = []

    def fake_verdicts(state, w, titles):
        judged.extend(titles)
        return {t: {"match": True, "year": None, "ram_gb": None, "model": None} for t in titles}

    monkeypatch.setattr(ai, "verdicts", fake_verdicts)
    monkeypatch.setattr(ai, "gate", lambda state, w, offers: {})
    Shop.items = [Offer(store="shop", title="Telefon mobil Samsung Galaxy S25, 12GB RAM", url="https://shop.ro/a", price=3000, offer_id="a"),
                  Offer(store="shop", title="Dispenser sapun Pouce, alb", url="https://shop.ro/b", price=5, offer_id="b")]
    tg = TG()
    e = Engine(Config(Settings(), []), State.load(""), tg)
    e.reg, e.store_names = {"shop": Shop}, {"shop": "Shop"}
    cp = CommandProcessor(e)
    tg.updates = [msg(1, "/start"), msg(2, "samsung galaxy s25 до 5000")]
    cp.poll()
    e.run_cycle()
    assert any("Galaxy S25" in t for t in judged) and not any("Dispenser" in t for t in judged)
