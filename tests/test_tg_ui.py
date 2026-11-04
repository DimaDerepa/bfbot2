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
