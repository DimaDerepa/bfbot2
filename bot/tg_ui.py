"""Button-based Telegram UI: main menu, per-product cards with inline buttons, step-by-step input.

Flow: "➕ Добавить товар" -> user types what to search -> product card with buttons
[💰 Макс. цена] [💵 Мин. цена] [🏷 Скидка] [🚫 Исключить] [✅ Обязательно] [🏪 Магазины] [📦 Наличие] ...
Tapping a value button asks for the value; the next text message from that chat sets it and the card refreshes.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional

from .config import parse_watch_spec, watch_from_dict
from .models import Watch
from .notify import esc
from .prices import fmt_price

log = logging.getLogger("bot.tg_ui")

BTN_ADD = "➕ Добавить товар"
BTN_LIST = "📋 Мои товары"
BTN_STORES = "🏪 Магазины"
BTN_HELP = "ℹ️ Помощь"

MAIN_KB = {
    "keyboard": [[{"text": BTN_ADD}, {"text": BTN_LIST}], [{"text": BTN_STORES}, {"text": BTN_HELP}]],
    "resize_keyboard": True,
    "is_persistent": True,
}

FIELDS = {
    "max_price": ("💰 Макс. цена", "Введите <b>максимальную цену</b> в леях, например <code>4800</code>.\n<code>0</code> — убрать ограничение."),
    "min_price": ("💵 Мин. цена", "Введите <b>минимальную цену</b> в леях (отсекает аксессуары), например <code>2000</code>.\n<code>0</code> — убрать."),
    "min_discount": ("🏷 Скидка %", "Введите <b>минимальную скидку</b> в процентах, например <code>20</code>.\n<code>0</code> — не важно."),
    "exclude": ("🚫 Исключить слова", "Слова, которых <b>не должно быть</b> в названии, через запятую.\nНапример: <code>max, resigilat, husa</code>\n<code>-</code> — очистить."),
    "must_include": ("✅ Обязательные слова", "Слова, которые <b>обязательно</b> должны быть в названии, через запятую.\nНапример: <code>256gb, oled</code>\n<code>-</code> — очистить (тогда обязательны все слова запроса)."),
    "any_of": ("🔀 Хотя бы одно из", "Слова, из которых <b>хотя бы одно</b> должно быть в названии, через запятую.\nНапример: <code>lg, sony, samsung</code>\n<code>-</code> — очистить."),
    "query": ("✏️ Что искать", "Напишите новый <b>поисковый запрос</b> латиницей, как на сайте магазина.\nНапример: <code>iphone 16 pro 256gb</code>"),
}


def _h(watch_id: str) -> str:
    return hashlib.sha1(watch_id.encode()).hexdigest()[:8]


def _num(text: str) -> Optional[float]:
    s = text.strip().lower().replace("лей", "").replace("lei", "").replace("ron", "").replace("%", "").strip()
    mult = 1
    if re.search(r"(к|k|тыс)\.?$", s):
        mult = 1000
        s = re.sub(r"(к|k|тыс)\.?$", "", s).strip()
    s = s.replace(" ", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        s = re.sub(r"[.,]", "", s)
    s = s.replace(",", ".")
    try:
        return float(s) * mult
    except ValueError:
        return None


def _words(text: str) -> list[str]:
    t = text.strip()
    if t in ("-", "—", "0", "нет", "очистить"):
        return []
    parts = [p.strip() for p in re.split(r"[,;\n]", t)] if re.search(r"[,;\n]", t) else t.split()
    return [p.strip("\"“”«»'") for p in parts if p.strip("\"“”«»'")]


class TelegramUI:
    def __init__(self, cp) -> None:
        self.cp = cp
        self.engine = cp.engine
        self.state = cp.state
        self.tg = cp.tg

    # ------------------------------------------------------------------ low level
    def send(self, chat_id: str, text: str, markup: Optional[dict] = None) -> bool:
        return self.tg.send(text, chat_id=chat_id, markup=markup)

    def edit(self, chat_id: str, message_id: Optional[int], text: str, markup: Optional[dict] = None) -> None:
        if not message_id:
            self.send(chat_id, text, markup)
            return
        params: dict[str, Any] = dict(chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
                                      link_preview_options={"is_disabled": True})
        if markup:
            params["reply_markup"] = markup
        res = self.tg.api("editMessageText", **params)
        if not res.get("ok") and "not modified" not in str(res.get("description", "")):
            self.send(chat_id, text, markup)

    def _pending(self) -> dict:
        return self.state.data["telegram"].setdefault("pending", {})

    # ------------------------------------------------------------------ watches
    def _find(self, h: str) -> Optional[Watch]:
        for w in self.engine.all_watches():
            if _h(w.id) == h:
                return w
        return None

    def _update(self, w: Watch, changes: dict) -> Watch:
        """Apply field changes to a Telegram watch (state) or a config watch (overrides). Validates first."""
        if w.origin == "telegram":
            for d in self.state.data["watches"]:
                if d.get("id") == w.id:
                    trial = {**{k: v for k, v in d.items() if k != "origin"}, **changes}
                    watch_from_dict(trial, origin="telegram")  # raises ValueError on bad input
                    d.update(changes)
        else:
            spec = {k: v for k, v in w.to_dict().items() if k != "origin"}
            watch_from_dict({**spec, **changes}, origin="config")
            self.state.data.setdefault("overrides", {}).setdefault(w.id, {}).update(changes)
        self.cp.force_check = True
        return self._find(_h(w.id)) or w

    def _create(self, text: str) -> Watch:
        spec = parse_watch_spec(text)
        if not spec.get("query") and not spec.get("urls"):
            raise ValueError("Не понял, что искать. Напишите, например: <code>iphone 16 pro 256gb</code>")
        spec.setdefault("name", spec.get("query") or spec["urls"][0])
        w = watch_from_dict(spec, origin="telegram")
        existing = {x.id for x in self.engine.all_watches()}
        base, n = w.id, 2
        while w.id in existing:
            w.id = f"{base}-{n}"
            n += 1
        st = self.state.data
        self.state.forget_watch(w.id)
        d = w.to_dict()
        d.pop("origin", None)
        st["watches"].append(d)
        self.cp.force_check = True
        return w

    # ------------------------------------------------------------------ rendering
    def card_text(self, w: Watch) -> str:
        cats = ""
        if not w.stores and not w.categories and w.query:
            try:
                from .categorize import infer_categories

                c = sorted(infer_categories(w.query))
                cats = f" (авто: {', '.join(c)})" if c else ""
            except Exception:
                pass
        price = "любая"
        if w.max_price is not None and w.min_price is not None:
            price = f"{fmt_price(w.min_price)} – {fmt_price(w.max_price)} лей"
        elif w.max_price is not None:
            price = f"до {fmt_price(w.max_price)} лей"
        elif w.min_price is not None:
            price = f"от {fmt_price(w.min_price)} лей"
        lines = [
            f"📦 <b>{esc(w.name)}</b>{'' if w.enabled else '  ⏸ на паузе'}",
            f"🔎 Запрос: <code>{esc(w.query or '—')}</code>" + (f" + {len(w.urls)} ссылк." if w.urls else ""),
            f"💰 Цена: {price}",
            f"🏷 Скидка: {('от ' + format(w.min_discount, 'g') + '%') if w.min_discount else 'не важно'}",
            f"🚫 Исключить: {esc(', '.join(w.exclude)) or '—'}",
            f"✅ Обязательно: {esc(', '.join(w.must_include)) or 'все слова запроса'}",
        ]
        if w.any_of:
            lines.append(f"🔀 Хотя бы одно из: {esc(', '.join(w.any_of))}")
        stores = ", ".join(self.engine.store_names.get(s, s) for s in w.stores) if w.stores else "все подходящие" + cats
        lines.append(f"🏪 Магазины: {esc(stores)}")
        lines.append(f"📦 Наличие: {'только в наличии' if w.in_stock else 'все, даже нет в наличии'}")
        best = self.state.best_offers(w, 1)
        if best:
            b = best[0]
            lines.append(f"\n⭐ Сейчас лучшая: <b>{fmt_price(b['p'])} лей</b> — {esc(self.engine.store_names.get(b['s'], b['s']))}\n"
                         f"<a href=\"{esc(b['u'])}\">{esc(b['t'][:90])}</a>")
        return "\n".join(lines)

    def card_kb(self, w: Watch) -> dict:
        h = _h(w.id)
        b = lambda text, act: {"text": text, "callback_data": f"{act}:{h}"}  # noqa: E731
        return {"inline_keyboard": [
            [b("💰 Макс. цена", "f.max_price"), b("💵 Мин. цена", "f.min_price")],
            [b("🏷 Скидка %", "f.min_discount"), b("🚫 Исключить", "f.exclude")],
            [b("✅ Обязательно", "f.must_include"), b("🔀 Одно из", "f.any_of")],
            [b("🏪 Магазины", "st"), b("📦 Наличие", "stock")],
            [b("✏️ Запрос", "f.query"), b("📊 Топ-10", "top")],
            [b("▶️ Включить" if not w.enabled else "⏸ Пауза", "pz"), b("🗑 Удалить", "del")],
            [{"text": "⬅️ Все товары", "callback_data": "list"}],
        ]}

    def list_view(self) -> tuple[str, dict]:
        ws = self.engine.all_watches()
        if not ws:
            return ("Пока ничего не отслеживается.\nНажмите <b>➕ Добавить товар</b>.",
                    {"inline_keyboard": [[{"text": BTN_ADD, "callback_data": "add"}]]})
        rows = []
        for i, w in enumerate(ws, 1):
            best = self.state.best_offers(w, 1)
            tail = f" · {fmt_price(best[0]['p'])}" if best else ""
            lim = f" ≤{fmt_price(w.max_price)}" if w.max_price else ""
            mark = "" if w.enabled else "⏸ "
            rows.append([{"text": f"{mark}{i}. {w.name[:34]}{lim}{tail}", "callback_data": f"o:{_h(w.id)}"}])
        rows.append([{"text": BTN_ADD, "callback_data": "add"}])
        return "<b>Ваши товары</b> — нажмите, чтобы настроить:", {"inline_keyboard": rows}

    def stores_kb(self, w: Watch) -> dict:
        h = _h(w.id)
        sel = set(w.stores or [])
        btns = []
        for k, cls in sorted(self.engine.reg.items(), key=lambda kv: (not kv[1].aggregator, kv[1].name.lower())):
            mark = "✅" if k in sel else "▫️"
            btns.append({"text": f"{mark} {cls.name}", "callback_data": f"sk:{h}:{k}"[:64]})
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        rows.append([{"text": ("🔘" if not sel else "⚪") + " Все подходящие (авто)", "callback_data": f"sa:{h}"}])
        rows.append([{"text": "✔️ Готово", "callback_data": f"o:{h}"}])
        return {"inline_keyboard": rows}

    # ------------------------------------------------------------------ entry points
    def start(self, chat_id: str, intro: str = "") -> None:
        self.send(chat_id, (intro + "\n\n" if intro else "") +
                  "Я слежу за ценами в магазинах Румынии и пишу, когда товар подходит под ваши условия.\n\n"
                  "Нажмите <b>➕ Добавить товар</b>, напишите, что искать, а дальше настройте цену, скидку и магазины кнопками.",
                  MAIN_KB)

    def handle_text(self, chat_id: str, text: str) -> bool:
        """Menu buttons and answers to a pending question. Returns True if the message was consumed."""
        if text == BTN_ADD:
            return self._ask_new(chat_id)
        if text == BTN_LIST:
            t, kb = self.list_view()
            self.send(chat_id, t, kb)
            return True
        if text == BTN_STORES:
            self.send(chat_id, self.cp.cmd_stores(""), MAIN_KB)
            return True
        if text == BTN_HELP:
            self.send(chat_id, self.cp.handle(chat_id, "/help") or "", MAIN_KB)
            return True
        pend = self._pending().get(chat_id)
        if not pend or text.startswith("/"):
            return False
        self._pending().pop(chat_id, None)
        try:
            if pend["field"] == "new":
                w = self._create(text)
                self.send(chat_id, "✅ Добавлено! Настройте условия кнопками ниже. Первые результаты пришлю после ближайшей проверки.\n\n"
                          + self.card_text(w), self.card_kb(w))
                return True
            w = self._find(pend["h"])
            if not w:
                self.send(chat_id, "Этот товар уже удалён.", MAIN_KB)
                return True
            field = pend["field"]
            if field in ("max_price", "min_price", "min_discount"):
                v = _num(text)
                if v is None:
                    self._pending()[chat_id] = pend
                    self.send(chat_id, "Нужно число, например <code>4800</code>. Попробуйте ещё раз:")
                    return True
                changes = {field: (v if v > 0 else None)}
            elif field == "query":
                q = text.strip()
                changes = {"query": q, "name": q} if w.origin == "telegram" else {"query": q}
            else:
                changes = {field: _words(text)}
            w = self._update(w, changes)
            self.edit(chat_id, pend.get("msg"), "✏️ Обновлено.\n\n" + self.card_text(w), self.card_kb(w))
        except ValueError as e:
            self.send(chat_id, f"⚠️ {e}")
        return True

    def _ask_new(self, chat_id: str) -> bool:
        self._pending()[chat_id] = {"field": "new"}
        self.send(chat_id, "Что искать? Напишите название <b>латиницей, как на сайте магазина</b>:\n"
                           "<code>iphone 16 pro 256gb</code> · <code>rtx 5070</code> · <code>aspirator dyson v15</code>\n\n"
                           "Можно сразу с условиями: <code>ps5 slim до 2300 -digital</code>\n"
                           "Или пришлите ссылку на конкретный товар в любом магазине.")
        return True

    def handle_callback(self, cq: dict) -> None:
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        mid = msg.get("message_id")
        self.tg.api("answerCallbackQuery", callback_query_id=cq.get("id"))
        if not chat_id:
            return
        parts = data.split(":")
        act = parts[0]
        if act == "add":
            self._ask_new(chat_id)
            return
        if act == "list":
            t, kb = self.list_view()
            self.edit(chat_id, mid, t, kb)
            return
        w = self._find(parts[1]) if len(parts) > 1 else None
        if w is None:
            t, kb = self.list_view()
            self.edit(chat_id, mid, "Этот товар не найден (возможно, удалён).\n\n" + t, kb)
            return
        try:
            if act == "o":
                self.edit(chat_id, mid, self.card_text(w), self.card_kb(w))
            elif act.startswith("f."):
                field = act[2:]
                label, prompt = FIELDS[field]
                self._pending()[chat_id] = {"field": field, "h": _h(w.id), "msg": mid}
                self.send(chat_id, f"<b>{esc(w.name)}</b> — {label}\n{prompt}")
            elif act == "stock":
                w = self._update(w, {"in_stock": not w.in_stock})
                self.edit(chat_id, mid, self.card_text(w), self.card_kb(w))
            elif act == "pz":
                self.cp._set_enabled(w, not w.enabled)
                w = self._find(_h(w.id)) or w
                self.edit(chat_id, mid, self.card_text(w), self.card_kb(w))
            elif act == "top":
                self.send(chat_id, self.cp.cmd_top(w.id))
            elif act == "del":
                self.edit(chat_id, mid, f"Удалить <b>{esc(w.name)}</b>?", {"inline_keyboard": [[
                    {"text": "🗑 Да, удалить", "callback_data": f"dely:{_h(w.id)}"},
                    {"text": "Отмена", "callback_data": f"o:{_h(w.id)}"}]]})
            elif act == "dely":
                res = self.cp.cmd_del(w.id)
                t, kb = self.list_view()
                self.edit(chat_id, mid, res + "\n\n" + t, kb)
            elif act == "st":
                self.edit(chat_id, mid, f"<b>{esc(w.name)}</b> — выберите магазины (✅ = опрашивать).\n"
                                        "«Все подходящие» — бот сам выберет магазины по типу товара.", self.stores_kb(w))
            elif act == "sk" and len(parts) > 2:
                sel = list(w.stores or [])
                k = parts[2]
                sel = [s for s in sel if s != k] if k in sel else sel + [k]
                w = self._update(w, {"stores": sel or None})
                self.edit(chat_id, mid, f"<b>{esc(w.name)}</b> — выберите магазины (✅ = опрашивать).", self.stores_kb(w))
            elif act == "sa":
                w = self._update(w, {"stores": None})
                self.edit(chat_id, mid, self.card_text(w), self.card_kb(w))
        except ValueError as e:
            self.send(chat_id, f"⚠️ {e}")
