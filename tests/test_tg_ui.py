from bot.config import Config, Settings
from bot.engine import Engine
from bot.notify import Telegram
from bot.state import State
from bot.tg_commands import CommandProcessor
from bot.tg_ui import _h


class TG(Telegram):
    def __init__(self):
        super().__init__("T", ())
        self.calls, self.updates = [], []

    def api(self, method, _http_timeout=30, **p):
        if method == "getUpdates":
            res = [u for u in self.updates if u["update_id"] >= p.get("offset", 0)]
            return {"ok": True, "result": res}
        self.calls.append((method, p))
        return {"ok": True, "result": {"message_id": 7}}

    def send(self, text, chat_id=None, preview=False, silent=False, markup=None):
        self.calls.append(("send", {"text": text, "chat_id": chat_id}))
        return True


def msg(uid, text, chat=5):
    return {"update_id": uid, "message": {"text": text, "chat": {"id": chat}}}


def btn(uid, data, chat=5):
    return {"update_id": uid, "callback_query": {"id": f"c{uid}", "data": data, "message": {"message_id": 7, "chat": {"id": chat}}}}


def texts(tg):
    return [p.get("text", "") for m, p in tg.calls if m in ("sendMessage", "editMessageText", "send")]


def test_button_flow():
    tg = TG()
    e = Engine(Config(Settings(), []), State.load(""), tg)
    cp = CommandProcessor(e)
    tg.updates = [msg(1, "привет"), msg(2, "➕ Добавить товар"), msg(3, "iphone 16 pro 256gb")]
    cp.poll()
    assert e.state.get("telegram", "chat_id") == "5"
    w = e.all_watches()[0]
    assert w.query == "iphone 16 pro 256gb"
    h = _h(w.id)
    tg.updates = [btn(4, f"f.max_price:{h}"), msg(5, "4 800"), btn(6, f"f.exclude:{h}"), msg(7, "max, resigilat"),
                  btn(8, f"sk:{h}:altex"), btn(9, f"stock:{h}")]
    cp.poll()
    w = e.all_watches()[0]
    assert w.max_price == 4800 and w.exclude == ["max", "resigilat"] and w.stores == ["altex"] and w.in_stock is False
    assert any("4 800" in t for t in texts(tg))
    # stranger cannot press buttons
    tg.updates = [btn(10, f"del:{h}", chat=666), btn(11, f"dely:{h}")]
    cp.poll()
    assert e.all_watches() == []
    assert any(m == "answerCallbackQuery" and "приватный" in p.get("text", "") for m, p in tg.calls)


def test_natural_language_parameter_search():
    from bot import fx
    from bot.matcher import evaluate
    from bot.models import Offer

    fx._cache["t"] = 9e18  # use fallback rate, no network
    tg = TG()
    e = Engine(Config(Settings(), []), State.load(""), tg)
    cp = CommandProcessor(e)
    tg.updates = [msg(1, "/start"), msg(2, "телефон 12 гб оперативки год выпуска 2025 и позже не моторола до 1000 долларов скидка больше 20%")]
    cp.poll()
    w = e.all_watches()[0]
    assert (w.query, w.ram_min, w.year_min, w.exclude, w.min_discount, w.pages) == ("telefon 12gb ram", 12, 2025, ["motorola"], 20.0, 3)
    assert 4000 < w.max_price < 5000
    ok = lambda t, p=3000, o=4500: evaluate(w, Offer(store="x", title=t, url="u", price=p, old_price=o, in_stock=True)).ok  # noqa: E731
    assert ok("Telefon mobil Samsung Galaxy S25, 12GB RAM, 256GB, 5G")
    assert ok("Telefon Xiaomi Redmi Note 14 Pro 5G, 12/512GB")
    assert ok("Telefon Apple iPhone 17 Pro 256GB")                       # RAM from the model table
    assert not ok("Telefon Motorola edge 60, 12GB RAM, 512GB")           # excluded brand
    assert not ok("Telefon Samsung Galaxy A55 5G, 8GB RAM, 256GB")       # 8 GB, 2024
    assert not ok("Telefon Samsung Galaxy S24, 12GB RAM, 256GB")         # 2024 model
    assert not ok("Telefon Samsung Galaxy S25, 12GB RAM, 256GB", 3000, 3300)   # discount < 20%
    assert not ok("Husa telefon Samsung Galaxy S25 12GB", 50, 90)        # accessory
    nl = __import__("bot.nlp_ru", fromlist=["parse"]).parse("ноутбук 16гб озу 1 тб до 5000 лей asus или lenovo")
    assert nl["ram_min"] == 16 and nl["any_of"] == ["asus", "lenovo"] and "1tb" in nl["must_include"]


def test_ai_watch_uses_claude_verdicts(monkeypatch):
    from bot import ai
    from bot.models import Offer
    from bot.stores.base import Store

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    calls = []

    def fake_json_call(model, system, user, schema, max_tokens, effort):
        calls.append(user)
        if "Titles:" not in user:   # request parsing
            return {"search_queries": ["telefon 12gb ram", "smartphone 12gb"], "product_type": "telefon", "category": "electronics",
                    "max_price": 1000, "min_price": None, "currency": "USD", "min_discount_pct": 20, "ram_min_gb": 12,
                    "storage_min_gb": None, "year_min": 2025, "brands_include": [], "brands_exclude": ["motorola"],
                    "exclude_words": [], "other_requirements": "", "summary_ru": "Телефон 12 ГБ, 2025+"}
        titles = [l.split(". ", 1)[1] for l in user.split("Titles:\n", 1)[1].splitlines()]
        return {"items": [{"i": i, "match": "Galaxy S25" in t, "year": 2025, "ram_gb": 12, "why": ""} for i, t in enumerate(titles)]}

    monkeypatch.setattr(ai, "_json_call", fake_json_call)

    class Shop(Store):
        key, name, base_url, categories = "shop", "Shop", "https://shop.ro", ("general",)

        def search_page(self, q, page):
            return [Offer(store="shop", title="Telefon mobil Samsung Galaxy S25, 12GB RAM, 256GB", url="https://shop.ro/1", price=3000, old_price=4000, offer_id="1"),
                    Offer(store="shop", title="Telefon Unknown Phone X 12GB", url="https://shop.ro/2", price=900, old_price=2000, offer_id="2")] if page == 1 else []

    tg = TG()
    e = Engine(Config(Settings(), []), State.load(""), tg)
    e.reg, e.store_names = {"shop": Shop}, {"shop": "Shop"}
    cp = CommandProcessor(e)
    tg.updates = [msg(1, "/start"), msg(2, "телефон 12 гб оперативки 2025+ не моторола до 1000$ скидка больше 20%")]
    cp.poll()
    w = e.all_watches()[0]
    assert w.ai and w.query == "telefon 12gb ram" and w.extra_queries == ["smartphone 12gb"] and w.exclude == ["motorola"]
    e.run_cycle()
    digest = [t for t in texts(tg) if "найдено" in t][-1]
    assert "Galaxy S25" in digest and "Unknown Phone" not in digest
    n = len(calls)
    e.run_cycle()
    assert len(calls) == n       # verdicts cached: no new API calls for the same titles


def test_mini_app_roundtrip(monkeypatch):
    import base64
    import json

    from bot import fx

    fx._cache["t"] = 9e18
    monkeypatch.setenv("GITHUB_REPOSITORY", "DimaDerepa/bfbot2")
    tg = TG()
    e = Engine(Config(Settings(), []), State.load(""), tg)
    cp = CommandProcessor(e)
    tg.updates = [msg(1, "/start")]
    cp.poll()
    kb = [p.get("markup") for m, p in tg.calls if m == "send" and p.get("markup")]
    # TG.send stores markup? it records text/chat only -> inspect via ui directly
    url = cp.ui.main_kb()["keyboard"][0][0]["web_app"]["url"]
    assert url.startswith("https://dimaderepa.github.io/bfbot2/app/?d=")
    data = {"v": 1, "ops": [{"op": "add", "text": "rtx 5070 до 3200"}]}
    tg.updates = [{"update_id": 2, "message": {"chat": {"id": 5}, "web_app_data": {"data": json.dumps(data)}}}]
    cp.poll()
    w = e.all_watches()[0]
    assert w.query == "rtx 5070" and w.max_price == 3200
    h = _h(w.id)
    upd = {"v": 1, "ops": [{"op": "upd", "h": h, "f": {"mx": 700, "cur": "USD", "ex": ["ti"], "st": ["altex", "emag"], "y": 2025, "en": False}}]}
    tg.updates = [{"update_id": 3, "message": {"chat": {"id": 5}, "web_app_data": {"data": json.dumps(upd)}}}]
    cp.poll()
    w = e.all_watches()[0]
    assert w.max_price == round(700 * 4.45) and w.exclude == ["ti"] and w.stores == ["altex", "emag"] and w.year_min == 2025
    assert w.enabled is False
    payload = cp.ui.main_kb()["keyboard"][0][0]["web_app"]["url"].split("d=", 1)[1]
    pad = "=" * (-len(payload) % 4)
    snap = json.loads(base64.urlsafe_b64decode(payload + pad))
    assert snap["w"][0]["h"] == h and snap["w"][0]["mx"] == w.max_price and any(s[0] == "emag" for s in snap["s"])
    tg.updates = [{"update_id": 4, "message": {"chat": {"id": 5}, "web_app_data": {"data": json.dumps({"ops": [{"op": "del", "h": h}]})}}}]
    cp.poll()
    assert e.all_watches() == []
    assert any("Сохранено" in t for t in texts(tg))
