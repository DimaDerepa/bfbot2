import copy

from bot.config import Config, Settings, watch_from_dict
from bot.engine import Engine
from bot.models import Offer
from bot.net import BlockedError
from bot.notify import Telegram
from bot.state import State
from bot.stores.base import Store
from bot.tg_commands import CommandProcessor


class FakeTG(Telegram):
    def __init__(self, chat_ids=("42",)):
        super().__init__("TOKEN", chat_ids)
        self.messages: list[tuple[str, str]] = []
        self.updates: list[dict] = []

    def send(self, text, chat_id=None, preview=False, silent=False):
        self.messages.append((chat_id or ",".join(self.chat_ids), text))

    def api(self, method, _http_timeout=30, **params):
        if method == "getUpdates":
            res = [u for u in self.updates if u["update_id"] >= params.get("offset", 0)]
            return {"ok": True, "result": res}
        return {"ok": True}


class FakeStore(Store):
    key = "fake"
    name = "FakeShop"
    base_url = "https://fake.ro"
    categories = ("general",)
    offers: list[Offer] = []
    blocked = False

    def search_page(self, query, page):
        if FakeStore.blocked:
            raise BlockedError("blocked (HTTP 403)", 403)
        return [copy.copy(o) for o in FakeStore.offers] if page == 1 else []


def mk(title, price, old=None, stock=True, pid=None):
    return Offer(store="fake", title=title, url=f"https://fake.ro/{pid or title.replace(' ', '-')}", price=price,
                 old_price=old, in_stock=stock, offer_id=pid)


def engine_with(watches, state=None, tg=None, **settings):
    cfg = Config(Settings(**{"store_fail_alert_after": 2, "first_run_digest": 2, **settings}), watches)
    st = state or State.load("")
    e = Engine(cfg, st, tg or FakeTG())
    e.reg = {"fake": FakeStore}
    e.store_names = {"fake": "FakeShop"}
    return e


def test_full_alert_lifecycle():
    FakeStore.blocked = False
    FakeStore.offers = [
        mk("Consola Sony PlayStation 5 Slim", 2399, 2899, pid="a"),
        mk("Consola Sony PlayStation 5 Slim Digital", 1999, pid="b"),
        mk("Consola Sony PlayStation 5 Slim + 2 controllere", 2599, pid="c"),
        mk("Husa PlayStation 5 Slim", 99, pid="d"),          # accessory -> ignored
        mk("Consola Sony PlayStation 5 Pro", 3699, pid="e"),  # no "slim" -> ignored
    ]
    w = watch_from_dict({"name": "PS5 Slim", "query": "playstation 5 slim", "max_price": 2500})
    tg = FakeTG()
    e = engine_with([w], tg=tg)

    # run 1: digest with 2 cheapest of 2 matches (c is above max)
    s = e.run_cycle()
    assert s["matches"] == 2
    assert len(tg.messages) == 1 and "найдено 2" in tg.messages[0][1]
    assert "1 999" in tg.messages[0][1] and "Husa" not in tg.messages[0][1]

    # run 2: nothing changed -> silence
    tg.messages.clear()
    e.run_cycle()
    assert tg.messages == []

    # run 3: 1% drop (below 2% threshold) -> silence; 5% drop -> alert
    FakeStore.offers[0].price = 2380
    e.run_cycle()
    assert tg.messages == []
    FakeStore.offers[0].price = 2250
    e.run_cycle()
    assert len(tg.messages) == 1 and "цена снизилась" in tg.messages[0][1] and "было 2 399" in tg.messages[0][1]

    # run 4: new offer appears + "c" drops under max -> both announced in one message
    tg.messages.clear()
    FakeStore.offers.append(mk("Consola PlayStation 5 Slim Disc Edition", 2299, pid="f"))
    FakeStore.offers[2].price = 2450
    e.run_cycle()
    assert len(tg.messages) == 1
    msg = tg.messages[0][1]
    assert "Disc Edition" in msg and "2 controllere" in msg

    # run 5: "b" goes out of stock (unmatched), then comes back -> rematch alert
    tg.messages.clear()
    FakeStore.offers[1].in_stock = False
    e.run_cycle()
    assert tg.messages == []
    FakeStore.offers[1].in_stock = True
    e.run_cycle()
    assert len(tg.messages) == 1 and "снова" in tg.messages[0][1]


def test_store_block_warning_once_and_recovery():
    FakeStore.offers = [mk("Laptop ASUS TUF Gaming A15", 3999, pid="x")]
    FakeStore.blocked = True
    w = watch_from_dict({"name": "tuf", "query": "asus tuf a15"})
    tg = FakeTG()
    e = engine_with([w], tg=tg)
    e.run_cycle()
    assert tg.messages == []             # first run: blocked, watch not initialised, no warning yet
    e.run_cycle()
    assert len(tg.messages) == 1 and "не отвечают" in tg.messages[0][1] and "FakeShop" in tg.messages[0][1]
    e.run_cycle()
    assert len(tg.messages) == 1         # warned only once
    FakeStore.blocked = False
    e.run_cycle()
    texts = [m[1] for m in tg.messages]
    assert any("Снова работают" in t for t in texts)
    assert any("найдено 1" in t for t in texts)  # digest happens once the store works


def test_alert_cap_marks_rest_notified():
    FakeStore.blocked = False
    FakeStore.offers = []
    w = watch_from_dict({"name": "tv", "query": "televizor oled"})
    tg = FakeTG()
    e = engine_with([w], tg=tg, max_alerts_per_watch=3)
    e.run_cycle()  # digest: nothing yet
    FakeStore.offers = [mk(f"Televizor OLED model {i}", 3000 + i, pid=str(i)) for i in range(7)]
    tg.messages.clear()
    e.run_cycle()
    assert len(tg.messages) == 1 and "ещё 4" in tg.messages[0][1]
    tg.messages.clear()
    e.run_cycle()
    assert tg.messages == []             # the 4 extra offers do not trickle in later


def upd(uid, text, chat=42):
    return {"update_id": uid, "message": {"text": text, "chat": {"id": chat}}}


def test_telegram_commands():
    FakeStore.blocked = False
    FakeStore.offers = [mk("Aspirator robot Roborock S8", 1999, 2999, pid="r")]
    tg = FakeTG(chat_ids=())             # no TELEGRAM_CHAT_ID -> first /start binds
    cfgw = watch_from_dict({"name": "cfg watch", "query": "roborock s8", "max_price": 1500})
    e = engine_with([cfgw], tg=tg)
    cp = CommandProcessor(e)
    tg.updates = [upd(1, "/start"), upd(2, "/add roborock s8 до 2500 -pro"), upd(3, "/list", chat=42),
                  upd(4, "/add hacker", chat=666)]
    assert cp.poll() == 4
    assert e.state.get("telegram", "chat_id") == "42"
    assert e.state.get("telegram", "offset") == 5
    replies = {m[0]: m[1] for m in tg.messages}
    assert "Приватный".lower() in replies["666"].lower()
    assert len(e.state.data["watches"]) == 1
    ws = e.all_watches()
    assert [w.name for w in ws] == ["cfg watch", "roborock s8"]
    assert ws[1].max_price == 2500 and ws[1].exclude == ["pro"]

    # override config watch via /set, pause/resume, delete tg watch
    tg.updates = [upd(5, "/set 1 до 2100"), upd(6, "/pause 2"), upd(7, "/del 2")]
    cp.poll()
    ws = e.all_watches()
    assert ws[0].max_price == 2100 and ws[0].id == cfgw.id
    assert len(ws) == 1

    # cycle sends digest to the bound chat
    tg.messages.clear()
    e.run_cycle()
    assert tg.messages and tg.messages[0][0] == "42" and "Roborock" in tg.messages[0][1]
    tg.updates = [upd(8, "/top 1"), upd(9, "/stores"), upd(10, "/status")]
    tg.messages.clear()
    cp.poll()
    assert "1 999" in tg.messages[0][1]
    assert "FakeShop" in tg.messages[1][1]


def test_url_watch_skips_title_check(monkeypatch):
    FakeStore.blocked = False

    def product(self, url):
        return Offer(store="fake", title="Whatever exact product", url=url, price=999, in_stock=True)

    monkeypatch.setattr(FakeStore, "product", product)
    FakeStore.domains = ("fake.ro",)
    w = watch_from_dict({"name": "exact", "urls": ["https://fake.ro/p/123"], "max_price": 1000})
    tg = FakeTG()
    e = engine_with([w], tg=tg)
    e.run_cycle()
    assert tg.messages and "999" in tg.messages[0][1]
    FakeStore.domains = ()


def test_state_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    st = State.load(str(p))
    st.data["telegram"]["offset"] = 77
    st.save()
    assert State.load(str(p)).get("telegram", "offset") == 77
