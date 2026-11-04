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
