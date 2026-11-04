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

BTN_APP = "🎯 Открыть приложение"

MAIN_KB = {
    "keyboard": [[{"text": BTN_ADD}, {"text": BTN_LIST}], [{"text": BTN_STORES}, {"text": BTN_HELP}]],
    "resize_keyboard": True,
    "is_persistent": True,
}


_APP_URL_CACHE: dict = {}


def repo_slug() -> Optional[str]:
    import os

    repo = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("BOT_REPO") or ""
    return repo if "/" in repo else None


def app_url() -> Optional[str]:
    """Mini App page: BOT_APP_URL overrides, otherwise GitHub Pages of this repo (served from main /app/)."""
    import os

    url = os.environ.get("BOT_APP_URL")
    if url:
        return url
    repo = repo_slug()
    if not repo:
        return None
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/app/"

FIELDS = {
    "max_price": ("💰 Макс. цена", "Введите <b>максимальную цену</b> в леях, например <code>4800</code>.\n<code>0</code> — убрать ограничение."),
    "min_price": ("💵 Мин. цена", "Введите <b>минимальную цену</b> в леях (отсекает аксессуары), например <code>2000</code>.\n<code>0</code> — убрать."),
    "min_discount": ("🏷 Скидка %", "Введите <b>минимальную скидку</b> в процентах, например <code>20</code>.\n<code>0</code> — не важно."),
    "exclude": ("🚫 Исключить слова", "Слова, которых <b>не должно быть</b> в названии, через запятую.\nНапример: <code>max, resigilat, husa</code>\n<code>-</code> — очистить."),
    "must_include": ("✅ Обязательные слова", "Слова, которые <b>обязательно</b> должны быть в названии, через запятую.\nНапример: <code>256gb, oled</code>\n<code>-</code> — очистить (тогда обязательны все слова запроса)."),
    "any_of": ("🔀 Хотя бы одно из", "Слова, из которых <b>хотя бы одно</b> должно быть в названии, через запятую.\nНапример: <code>lg, sony, samsung</code>\n<code>-</code> — очистить."),
    "ram_min": ("🧠 ОЗУ от", "Введите <b>минимальный объём оперативной памяти</b> в ГБ, например <code>12</code>.\n<code>0</code> — не важно."),
    "year_min": ("📅 Год от", "Введите <b>минимальный год выпуска модели</b>, например <code>2025</code>.\n<code>0</code> — не важно."),
    "query": ("✏️ Что искать", "Напишите новый <b>поисковый запрос</b> латиницей, как на сайте магазина.\nНапример: <code>iphone 16 pro 256gb</code>"),
}


def _h(watch_id: str) -> str:
    return hashlib.sha1(watch_id.encode()).hexdigest()[:8]


def _money(text: str) -> Optional[float]:
    """'4800', '4 800 лей', '1000$', '900 евро', '1,2к' -> RON."""
    from .fx import currency_of, to_ron

    t = text.strip().lower()
    cur = None
    m = re.search(r"(\$|€|£|usd|eur|euro|евро|долл\w*|доллар\w*|бакс\w*|лей|леев|lei|ron)", t)
    if m:
        cur = currency_of(m.group(1))
        t = (t[:m.start()] + t[m.end():]).strip()
    v = _num(t)
    return None if v is None else to_ron(v, cur or "RON")


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
    BTN_APP_TEXT = BTN_APP
    _extra_created: list = []
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

    def main_kb(self) -> dict:
        """The only chat button: opens the Mini App (all features live there)."""
        url = app_url()
        if not url:
            return {"remove_keyboard": True}
        sep = "&" if "?" in url else "?"
        return {"keyboard": [[{"text": BTN_APP, "web_app": {"url": f"{url}{sep}d={self.webapp_payload()}"}}]],
                "resize_keyboard": True, "is_persistent": True}

    def webapp_payload(self) -> str:
        import base64
        import json

        raw = json.dumps(self.webapp_snapshot(full=False), ensure_ascii=False, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    def webapp_snapshot(self, full: bool = True) -> dict:
        """Everything the Mini App shows. The full version (top offers, shop health, settings) is stored in
        state.json ("app") and read live by the app from the public state branch; the short version rides in
        the keyboard button URL as a fallback."""
        import os
        import time as _t

        from .stores import registry

        ws = []
        for w in self.engine.all_watches():
            top = self.state.best_offers(w, 6 if full else 1)
            d = {"h": _h(w.id), "n": w.name[:60], "q": w.query or "", "xq": w.extra_queries,
                 "mn": w.min_price, "mx": w.max_price, "dc": w.min_discount, "r": w.ram_min, "y": w.year_min,
                 "ex": w.exclude, "inc": [x for x in w.must_include if not x.startswith("re:")], "any": w.any_of,
                 "st": w.stores, "stk": w.in_stock, "en": w.enabled, "ai": w.ai, "it": (w.intent or "")[:160],
                 "best": [top[0]["p"], top[0]["s"]] if top else None}
            if full:
                d["top"] = [[r.get("p"), r.get("op"), r.get("s"), (r.get("t") or "")[:110], r.get("u"), r.get("n", 1)] for r in top]
            ws.append(d)
        reg = registry(getattr(self.engine.settings, "shops", ()))
        home = set(getattr(self.engine.settings, "home_stores", []) or [])
        home_on = os.environ.get("BOT_HOME_ENABLED", "").lower() in ("1", "true", "yes")
        stores = [[k, c.name, 1 if c.aggregator else 0]
                  for k, c in sorted(reg.items(), key=lambda kv: (not kv[1].aggregator, kv[1].name.lower()))]
        snap: dict[str, Any] = {"v": 2, "t": int(_t.time()), "r": repo_slug(), "w": ws, "s": stores}
        if full:
            health = self.state.data.get("stores", {})
            hs = []
            for k, name, _agg in stores:
                h = health.get(k) or {}
                where = "home" if (k in home and home_on) else "cloud"
                status = "new" if not h else ("ok" if not h.get("cf") else "fail")
                hs.append([k, status, h.get("last_count"), (h.get("last_error") or "")[:90], where, h.get("last_ok")])
            st = self.engine.settings
            snap.update({
                "health": hs, "last_run": self.state.get("last_run"), "runs": self.state.get("runs"),
                "home_on": home_on, "ai_on": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "cfg": {"quiet": getattr(st, "quiet_hours", None), "drop": getattr(st, "notify_drop_pct", 2),
                        "digest": getattr(st, "first_run_digest", 8), "dedupe": bool(getattr(st, "dedupe_models", True))},
            })
        return snap

    def handle_webapp(self, chat_id: str, raw: str) -> None:
        """Changes sent by the Mini App (Telegram.WebApp.sendData)."""
        import json

        from .fx import to_ron

        try:
            data = json.loads(raw)
        except ValueError:
            self.send(chat_id, "⚠️ Не удалось прочитать данные из приложения.", self.main_kb())
            return
        done: list[str] = []
        for op in (data.get("ops") or [])[:40]:
            try:
                kind = op.get("op")
                if kind == "cfg":
                    ov = self.state.data.setdefault("settings_overrides", {})
                    c = op.get("f") or {}
                    if "quiet" in c:
                        q = str(c["quiet"] or "").strip()
                        ov["quiet_hours"] = q or None
                    if "drop" in c and c["drop"] is not None:
                        ov["notify_drop_pct"] = max(0.5, float(c["drop"]))
                    if "digest" in c and c["digest"]:
                        ov["first_run_digest"] = max(1, min(30, int(c["digest"])))
                    if "dedupe" in c and c["dedupe"] is not None:
                        ov["dedupe_models"] = bool(c["dedupe"])
                    for k, v in ov.items():
                        if hasattr(self.engine.settings, k):
                            setattr(self.engine.settings, k, v)
                    done.append("⚙️ Настройки сохранены")
                    continue
                if kind == "add" and str(op.get("text") or "").strip():
                    self._extra_created = []
                    w = self._create(str(op["text"]))
                    for x in [w, *self._extra_created]:
                        done.append(f"➕ {esc(x.name)} — {esc(x.describe())}")
                    continue
                w = self._find(str(op.get("h") or ""))
                if w is None:
                    continue
                if kind == "del":
                    self.cp.cmd_del(w.id)
                    done.append(f"🗑 {esc(w.name)}")
                elif kind == "upd":
                    f = op.get("f") or {}
                    cur = f.get("cur") or None
                    ch: dict[str, Any] = {}
                    if "q" in f and str(f["q"] or "").strip():
                        ch["query"] = str(f["q"]).strip()
                    if "xq" in f:
                        ch["extra_queries"] = [str(x) for x in f["xq"] or []]
                    for k, dst in (("mn", "min_price"), ("mx", "max_price")):
                        if k in f or ("cur" in f and getattr(w, dst) is not None):
                            v = f.get(k, getattr(w, dst))
                            ch[dst] = to_ron(float(v), cur) if v else None
                    if "dc" in f:
                        ch["min_discount"] = float(f["dc"]) if f["dc"] else None
                    for k, dst in (("r", "ram_min"), ("y", "year_min")):
                        if k in f:
                            ch[dst] = int(f[k]) if f[k] else None
                            if f[k]:
                                ch["pages"] = max(3, w.pages or 0)
                    if "ex" in f:
                        ch["exclude"] = [str(x) for x in f["ex"] or []]
                    if "inc" in f:
                        ch["must_include"] = [x for x in w.must_include if x.startswith("re:")] + [str(x) for x in f["inc"] or []]
                    if "any" in f:
                        ch["any_of"] = [str(x) for x in f["any"] or []]
                    if "st" in f:
                        ch["stores"] = [str(x) for x in f["st"]] if f["st"] else None
                    if "stk" in f:
                        ch["in_stock"] = bool(f["stk"])
                    if ch:
                        w = self._update(w, ch)
                    if "en" in f and bool(f["en"]) != w.enabled:
                        self.cp._set_enabled(w, bool(f["en"]))
                    done.append(f"✏️ {esc(w.name)} — {esc(w.describe())}")
            except (ValueError, TypeError) as e:
                done.append(f"⚠️ {esc(str(e))}")
        self.cp.force_check = True
        text = ("💾 Сохранено:\n" + "\n".join(done) + "\n\n" + self.cp._when()) if done else "Изменений нет."
        self.send(chat_id, text, self.main_kb())

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
        from . import nlp_ru

        spec = None
        extra_specs: list = []
        if not text.strip().lower().startswith(("http://", "https://")):
            from . import ai

            if ai.enabled():
                parsed = ai.parse_requests(text)
                if parsed:
                    spec = ai.spec_from_ai(parsed[0], text)
                    extra_specs = [ai.spec_from_ai(d, text) for d in parsed[1:]]   # several products -> one watch each
        if spec is None:
            spec = nlp_ru.parse(text) if nlp_ru.looks_natural(text) else parse_watch_spec(text)
        w = self._create_from_spec(spec)
        self._extra_created = []
        for x in extra_specs:
            try:
                self._extra_created.append(self._create_from_spec(x))
            except ValueError:
                pass
        return w

    def _create_from_spec(self, spec: dict) -> Watch:
        if not spec.get("query") and not spec.get("urls"):
            raise ValueError("Не понял, что искать. Напишите, например: <code>iphone 16 pro 256gb</code>")
        spec.setdefault("name", spec.get("query") or spec["urls"][0])
        w = watch_from_dict(spec, origin="telegram")
        for x in self.engine.all_watches():   # the same request twice (double tap, app + chat) -> keep one
            if (x.query or "") == (w.query or "") and x.max_price == w.max_price and x.min_price == w.min_price \
                    and sorted(x.exclude) == sorted(w.exclude) and x.urls == w.urls:
                raise ValueError(f"«{esc(x.name)}» уже отслеживается.")
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
            f"🧠 ОЗУ: {('от ' + str(w.ram_min) + ' ГБ') if w.ram_min else 'не важно'} · 📅 Год: {('от ' + str(w.year_min)) if w.year_min else 'не важно'}",
            f"🚫 Исключить: {esc(', '.join(w.exclude)) or '—'}",
            f"✅ Обязательно: {esc(', '.join(w.must_include)) or 'все слова запроса'}",
        ]
        if w.any_of:
            lines.append(f"🔀 Хотя бы одно из: {esc(', '.join(w.any_of))}")
        stores = ", ".join(self.engine.store_names.get(s, s) for s in w.stores) if w.stores else "все подходящие" + cats
        lines.append(f"🏪 Магазины: {esc(stores)}")
        lines.append(f"📦 Наличие: {'только в наличии' if w.in_stock else 'все, даже нет в наличии'}")
        if w.ai:
            from . import ai as _ai

            lines.append("🤖 Товары проверяет Claude (тип, год, ОЗУ, требования)." if _ai.enabled()
                         else "🤖 Claude не подключён (нет ANTHROPIC_API_KEY): работают обычные правила.")
            if w.extra_queries:
                lines.append("🔎 Ещё запросы: " + esc(", ".join(w.extra_queries)))
        elif w.ram_min or w.year_min:
            lines.append("<i>ОЗУ и год определяются по названию и модели; если их не понять, товар не показывается.</i>")
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
            [b("🧠 ОЗУ от", "f.ram_min"), b("📅 Год от", "f.year_min")],
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
                  f"Нажмите <b>{BTN_APP}</b> внизу чата: там все товары, лучшие цены, условия, магазины и настройки. "
                  "Можно и просто написать мне, что ищете, обычными словами.", self.main_kb())

    def handle_text(self, chat_id: str, text: str) -> bool:
        """Menu buttons and answers to a pending question. Returns True if the message was consumed."""
        if text == BTN_ADD:
            return self._ask_new(chat_id)
        if text in (BTN_LIST, BTN_STORES, BTN_HELP, "⚙️ Управление"):
            self.send(chat_id, f"Всё управление теперь в приложении — нажмите «{BTN_APP}» внизу чата.", self.main_kb())
            return True
        pend = self._pending().get(chat_id)
        if not pend or text.startswith("/"):
            return False
        self._pending().pop(chat_id, None)
        try:
            if pend["field"] == "new":
                self._extra_created = []
                w = self._create(text)
                extra = "".join(f"\n✅ Добавлено: <b>{esc(x.name)}</b>\n{esc(x.describe())}" for x in self._extra_created)
                self.send(chat_id, f"✅ Добавлено: <b>{esc(w.name)}</b>\n{esc(w.describe())}{extra}\n\n"
                          "Первые результаты пришлю после ближайшей проверки. Условия можно поменять в приложении — "
                          f"кнопка «{BTN_APP}» внизу.", self.main_kb())
                return True
            w = self._find(pend["h"])
            if not w:
                self.send(chat_id, "Этот товар уже удалён.", self.main_kb())
                return True
            field = pend["field"]
            if field in ("max_price", "min_price", "min_discount", "ram_min", "year_min"):
                v = _money(text) if field in ("max_price", "min_price") else _num(text)
                if v is None:
                    self._pending()[chat_id] = pend
                    self.send(chat_id, "Нужно число, например <code>4800</code> (можно <code>1000$</code> или <code>900€</code>). Попробуйте ещё раз:")
                    return True
                if field in ("ram_min", "year_min"):
                    changes = {field: int(v) if v > 0 else None}
                    if v > 0:
                        changes["pages"] = 3
                else:
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
        self.send(chat_id, "Что искать? Опишите <b>обычными словами</b>:\n"
                           "<code>телефон 12 гб оперативки 2025 и новее не моторола до 1000$ скидка больше 20%</code>\n"
                           "<code>ноутбук 16 гб озу до 4000 лей asus или lenovo</code>\n"
                           "<code>телевизор oled 55 до 4500</code> · <code>пылесос дайсон скидка 30%</code>\n\n"
                           "Или конкретную модель латиницей: <code>iphone 17 pro 256gb</code>, <code>rtx 5070</code>\n"
                           "Или ссылку на товар в любом магазине.")
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
