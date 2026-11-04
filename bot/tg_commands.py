"""Telegram command handling (/add, /list, /del, /set, /top, /stores, ...)."""
from __future__ import annotations

import logging
import time
from typing import Optional

from .config import parse_watch_spec, watch_from_dict
from .engine import Engine
from .models import Watch
from .notify import esc, esc_attr
from .prices import fmt_price

log = logging.getLogger("bot.tg")

HELP = """<b>Бот-охотник за скидками 🇷🇴</b>

<b>Добавить товар:</b>
<code>/add iphone 16 pro 256gb до 4800 -max -resigilat</code>
<code>/add rtx 5070 &lt;3200 &gt;2000 @pcgarage,emag,altex</code>
<code>/add aspirator dyson v15 %25</code>  — скидка от 25%
<code>/add https://www.emag.ro/... &lt;2000</code> — конкретная страница товара

<b>Критерии:</b>
• <code>до 4800</code> или <code>&lt;4800</code> — максимальная цена, лей
• <code>от 3000</code> или <code>&gt;3000</code> — минимальная цена (отсекает аксессуары)
• <code>%20</code> или <code>скидка 20</code> — минимальная скидка, %
• <code>-слово</code> / <code>-"два слова"</code> — исключить из названия
• <code>+слово</code> — обязательно в названии (вместо слов запроса)
• <code>?a|b|c</code> — хотя бы одно из
• <code>@emag,altex</code> — только эти магазины · <code>^cel</code> — кроме этих
• <code>#electronics</code> — категории магазинов (electronics, pc, home, appliances, fashion, beauty, diy, toys)
• <code>!all</code> — показывать и то, чего нет в наличии · <code>~</code> — нестрогое совпадение названия
По умолчанию все слова запроса должны быть в названии, а чехлы/плёнки/кабели отсекаются.

<b>Команды:</b>
/list — список отслеживаемого
/top N — самые дешёвые сейчас для пункта N
/set N до 4500 -max — изменить критерии пункта N
/pause N · /resume N · /del N
/stores — состояние магазинов
/status — когда была последняя проверка
"""


class CommandProcessor:
    def __init__(self, engine: Engine, mode: str = "actions") -> None:
        self.engine = engine
        self.state = engine.state
        self.tg = engine.tg
        self.mode = mode  # "actions" (cron) | "daemon"
        self.force_check = False
        from .tg_ui import TelegramUI

        self.ui = TelegramUI(self)

    # ------------------------------------------------------------------ polling
    def poll(self, timeout: int = 0) -> Optional[int]:
        """Fetch and process pending updates. Returns the number of processed messages,
        or None when getUpdates failed (the daemon then backs off)."""
        if not self.tg.enabled:
            return 0
        self._drop_stale_binding()
        offset = int(self.state.get("telegram", "offset", default=0) or 0)
        res = self.tg.api("getUpdates", _http_timeout=timeout + 15, offset=offset, timeout=timeout,
                          allowed_updates=["message", "callback_query"])
        if not res.get("ok"):
            desc = str(res.get("description", ""))
            if "webhook" in desc.lower():
                self.tg.api("deleteWebhook")
            return None
        n = 0
        for upd in res.get("result", []):
            self.state.data["telegram"]["offset"] = int(upd["update_id"]) + 1
            cq = upd.get("callback_query")
            if cq:
                cq_chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
                if self.authorized(cq_chat):
                    try:
                        self.ui.handle_callback(cq)
                    except Exception:
                        log.exception("button failed: %s", cq.get("data"))
                        self.tg.api("answerCallbackQuery", callback_query_id=cq.get("id"), text="Ошибка, попробуйте ещё раз")
                else:
                    self.tg.api("answerCallbackQuery", callback_query_id=cq.get("id"), text="⛔ Бот приватный")
                n += 1
                continue
            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            wad = (msg.get("web_app_data") or {}).get("data")
            if wad and chat_id:
                if self.authorized(chat_id):
                    try:
                        self.ui.handle_webapp(chat_id, wad)
                    except Exception:
                        log.exception("mini app data failed")
                        self.tg.send("⚠️ Не удалось применить изменения.", chat_id=chat_id)
                n += 1
                continue
            if not text or not chat_id:
                continue
            # ignore commands older than 2 days (e.g. bot was off)
            if msg.get("date") and time.time() - int(msg["date"]) > 2 * 86400:
                continue
            try:
                reply = self.handle(chat_id, text)
            except Exception as e:  # never crash the run because of a command
                log.exception("command failed: %s", text)
                reply = f"Ошибка: {esc(str(e))}"
            if reply:
                self.tg.send(reply, chat_id=chat_id)
            n += 1
        return n

    # ------------------------------------------------------------------ auth
    def authorized(self, chat_id: str) -> bool:
        """TELEGRAM_CHAT_ID, when set, is the only authority; otherwise the chat bound by the first /start."""
        if self.tg.chat_ids:
            return chat_id in set(self.tg.chat_ids)
        bound = self.state.get("telegram", "chat_id")
        return bool(bound) and chat_id == str(bound)

    def _drop_stale_binding(self) -> None:
        """The owner set TELEGRAM_CHAT_ID (e.g. after a stranger took the bot with /start): forget the old
        binding, so it cannot come back if the secret is removed later."""
        bound = self.state.get("telegram", "chat_id")
        if self.tg.chat_ids and bound and str(bound) not in set(self.tg.chat_ids):
            log.warning("TELEGRAM_CHAT_ID is set; dropping the chat bound by /start (%s)", bound)
            self.state.data["telegram"]["chat_id"] = None

    # ------------------------------------------------------------------ dispatch
    def handle(self, chat_id: str, text: str) -> Optional[str]:
        # the very first message of any kind binds the chat when TELEGRAM_CHAT_ID is not set
        if not self.tg.chat_ids and not self.state.get("telegram", "chat_id"):
            self.state.data["telegram"]["chat_id"] = chat_id
            self.ui.start(chat_id, "✅ Чат привязан — теперь бот отвечает только вам.")
            return None
        if not self.authorized(chat_id):
            return "⛔ Этот бот приватный."
        if not text.startswith("/"):
            if self.ui.handle_text(chat_id, text):
                return None
            # plain text without a pending question: treat it as a new product to watch
            self.ui._pending()[chat_id] = {"field": "new"}
            self.ui.handle_text(chat_id, text)
            return None
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/menu"):
            self.ui.start(chat_id)
            return None
        if cmd == "/list" and not arg:
            t, kb = self.ui.list_view()
            self.ui.send(chat_id, t, kb)
            return None
        if cmd == "/add" and not arg:
            self.ui._ask_new(chat_id)
            return None
        handler = {
            "/help": lambda a: HELP, "/add": self.cmd_add, "/list": self.cmd_list, "/del": self.cmd_del,
            "/delete": self.cmd_del, "/rm": self.cmd_del, "/pause": self.cmd_pause, "/resume": self.cmd_resume,
            "/set": self.cmd_set, "/top": self.cmd_top, "/stores": self.cmd_stores, "/status": self.cmd_status,
            "/check": self.cmd_check,
        }.get(cmd)
        if handler is None:
            return "Не знаю такую команду. /help"
        return handler(arg)

    # ------------------------------------------------------------------ helpers
    def _resolve(self, ref: str) -> Optional[Watch]:
        watches = self.engine.all_watches()
        ref = ref.strip()
        if ref.isdigit():
            i = int(ref) - 1
            return watches[i] if 0 <= i < len(watches) else None
        for w in watches:
            if w.id == ref or w.name.lower() == ref.lower():
                return w
        return None

    def _when(self) -> str:
        return "Проверяю прямо сейчас." if self.mode == "daemon" else "Результаты придут после ближайшей проверки (обычно в течение 10–15 минут)."

    # ------------------------------------------------------------------ commands
    def cmd_add(self, arg: str) -> str:
        if not arg:
            return "Пример: <code>/add iphone 16 pro 256gb до 4800 -max</code>"
        spec = parse_watch_spec(arg)
        if not spec.get("query") and not spec.get("urls"):
            return "Не понял, что искать. Пример: <code>/add iphone 16 pro до 4800</code>"
        spec.setdefault("name", spec.get("query") or spec["urls"][0])
        w = watch_from_dict(spec, origin="telegram")
        existing = {x.id for x in self.engine.all_watches()}
        base, n = w.id, 2
        while w.id in existing:
            w.id = f"{base}-{n}"
            n += 1
        # the id belongs to no current watch: whatever is stored under it is left over from a deleted watch
        # (/del then /add of the same name) and would hide the new watch's digest and known offers
        self.state.forget_watch(w.id)
        unknown = [s for s in (w.stores or []) + w.skip_stores if s not in self.engine.reg]
        d = w.to_dict()
        d.pop("origin", None)
        self.state.data["watches"].append(d)
        self.force_check = True
        idx = len(self.engine.all_watches())
        warn = f"\n⚠️ Неизвестные магазины: {esc(', '.join(unknown))} (см. /stores)" if unknown else ""
        return f"✅ Добавлено <b>#{idx}</b>: {esc(w.describe())}{warn}\n{self._when()}"

    def cmd_list(self, arg: str) -> str:
        watches = self.engine.all_watches()
        if not watches:
            return "Пока ничего не отслеживается. Добавьте: <code>/add название до 1000</code>"
        lines = ["<b>Отслеживаю:</b>"]
        for i, w in enumerate(watches, 1):
            best = self.state.best_offers(w, 1)
            best_s = f" — сейчас от <b>{fmt_price(best[0]['p'])}</b> ({esc(self.engine.store_names.get(best[0]['s'], best[0]['s']))})" if best else ""
            mark = "✅" if w.enabled else "⏸"
            src = "" if w.origin == "telegram" else " <i>[config]</i>"
            lines.append(f"{i}. {mark} <b>{esc(w.name)}</b>{src}\n    {esc(w.describe())}{best_s}")
        return "\n".join(lines)

    def cmd_del(self, arg: str) -> str:
        w = self._resolve(arg)
        if not w:
            return "Не нашёл такой пункт. /list"
        if w.origin == "telegram":
            self.state.data["watches"] = [d for d in self.state.data["watches"] if d.get("id") != w.id]
            self.state.forget_watch(w.id)
            return f"🗑 Удалено: {esc(w.name)}"
        if w.id not in self.state.data["disabled"]:
            self.state.data["disabled"].append(w.id)
        return f"⏸ «{esc(w.name)}» задан в config.yaml — я его отключил. Чтобы удалить навсегда, уберите его из файла."

    def _set_enabled(self, w: Watch, enabled: bool) -> None:
        if w.origin == "telegram":
            for d in self.state.data["watches"]:
                if d.get("id") == w.id:
                    d["enabled"] = enabled
        else:
            dis = self.state.data["disabled"]
            if enabled and w.id in dis:
                dis.remove(w.id)
            elif not enabled and w.id not in dis:
                dis.append(w.id)

    def cmd_pause(self, arg: str) -> str:
        w = self._resolve(arg)
        if not w:
            return "Не нашёл такой пункт. /list"
        self._set_enabled(w, False)
        return f"⏸ Пауза: {esc(w.name)}"

    def cmd_resume(self, arg: str) -> str:
        w = self._resolve(arg)
        if not w:
            return "Не нашёл такой пункт. /list"
        self._set_enabled(w, True)
        self.force_check = True
        return f"▶️ Снова слежу: {esc(w.name)}"

    def cmd_set(self, arg: str) -> str:
        ref, _, rest = arg.partition(" ")
        w = self._resolve(ref)
        if not w or not rest.strip():
            return "Пример: <code>/set 2 до 4500 -max</code>"
        spec = parse_watch_spec(rest)
        spec.pop("name", None)
        # validate the result BEFORE storing it: a rejected change (bad link, Cyrillic words) must leave the watch
        # as it was, not half-applied in state (Telegram watch) or silently ignored later (config override)
        if w.origin == "telegram":
            for d in self.state.data["watches"]:
                if d.get("id") == w.id:
                    watch_from_dict({**{k: v for k, v in d.items() if k != "origin"}, **spec}, origin="telegram")
                    d.update(spec)
        else:
            spec_now = {k: v for k, v in w.to_dict().items() if k != "origin"}   # overrides already applied
            watch_from_dict({**spec_now, **spec}, origin="config")
            self.state.data.setdefault("overrides", {}).setdefault(w.id, {}).update(spec)
        self.force_check = True
        w2 = self._resolve(w.id)
        return f"✏️ Обновлено: {esc(w2.describe() if w2 else w.name)}"

    def cmd_top(self, arg: str) -> str:
        w = self._resolve(arg or "1")
        if not w:
            return "Не нашёл такой пункт. /list"
        rows = self.state.best_offers(w, 10)
        if not rows:
            return f"По «{esc(w.name)}» сейчас нет подходящих предложений."
        lines = [f"<b>{esc(w.name)}</b> — самые дешёвые сейчас:"]
        for r in rows:
            old = f" <s>{fmt_price(r['op'])}</s>" if r.get("op") and r["op"] > r["p"] else ""
            store = esc(self.engine.store_names.get(r["s"], r["s"]))
            lines.append(f"• <b>{fmt_price(r['p'])} лей</b>{old} — {store}\n  <a href=\"{esc_attr(r['u'])}\">{esc(r['t'][:120])}</a>")
        return "\n".join(lines)

    def cmd_stores(self, arg: str) -> str:
        health = self.state.data.get("stores", {})
        lines = ["<b>Магазины:</b>"]
        for k, cls in sorted(self.engine.reg.items()):
            h = health.get(k)
            if not h:
                st = "⚪ ещё не опрашивался"
            elif h.get("cf", 0) == 0:
                st = f"🟢 ок, последн. результатов: {h.get('last_count', '?')}"
            else:
                st = f"🔴 ошибок подряд: {h['cf']} — {esc(h.get('last_error', '')[:80])}"
            agg = " (агрегатор)" if cls.aggregator else ""
            lines.append(f"<code>{esc(k)}</code> {esc(cls.name)}{agg}: {st}")
        return "\n".join(lines)

    def cmd_status(self, arg: str) -> str:
        last = self.state.get("last_run")
        ago = f"{int((time.time() - last) / 60)} мин назад" if last else "ещё не было"
        s = self.state.get("last_summary", default={}) or {}
        n_active = len(self.engine.active_watches())
        return (f"Последняя проверка: {ago}\nВсего проверок: {self.state.get('runs', default=0)}\n"
                f"Активных пунктов: {n_active}\nПодходящих предложений в последний раз: {s.get('matches', 0)}\n"
                f"Длительность проверки: {s.get('seconds', '?')} с")

    def cmd_check(self, arg: str) -> str:
        self.force_check = True
        return "🔄 " + self._when()
