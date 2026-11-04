"""Telegram delivery + message formatting (Russian UI)."""
from __future__ import annotations

import datetime as _dt
import html
import logging
import os
import re
import time
from typing import Iterable, Optional

import requests

from .prices import fmt_price
from .state import Alert

log = logging.getLogger("bot.notify")

TG_LIMIT = 4000

KIND_ICON = {"new": "🆕", "drop": "📉", "back": "✅", "rematch": "🔁"}
KIND_TEXT = {"new": "новое", "drop": "цена снизилась", "back": "снова в наличии", "rematch": "снова подходит"}


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def esc_attr(s: str) -> str:
    """Escape a value for an HTML attribute (href="...")."""
    return html.escape(s or "", quote=True)


class Telegram:
    def __init__(self, token: Optional[str], chat_ids: Iterable[str] = ()):
        self.token = token or None
        self.chat_ids = [str(c) for c in chat_ids if str(c).strip()]
        self.sent = 0
        self._s = requests.Session()

    @classmethod
    def from_env(cls) -> "Telegram":
        ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
        return cls(os.environ.get("TELEGRAM_BOT_TOKEN"), ids)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def api(self, method: str, _http_timeout: float = 30, **params) -> dict:
        """Call a Bot API method. Retries network errors, 429 (after retry_after) and 5xx; never raises."""
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        attempts = 4
        last: dict = {"ok": False, "description": "network error"}
        for attempt in range(attempts):
            more = attempt < attempts - 1
            try:
                r = self._s.post(url, json=params, timeout=_http_timeout)
            except requests.RequestException as e:
                log.warning("telegram %s failed: %s", method, e)
                last = {"ok": False, "description": f"network error: {type(e).__name__}"}
                if more:
                    time.sleep(2 * (attempt + 1))
                continue
            try:
                data = r.json()
                if not isinstance(data, dict):
                    raise ValueError("not an object")
            except ValueError:
                data = {"ok": False, "error_code": r.status_code, "description": (r.text or "")[:200]}
            if r.status_code == 429:
                last = data
                wait = (data.get("parameters") or {}).get("retry_after", 5)
                try:
                    wait = float(wait)
                except (TypeError, ValueError):
                    wait = 5.0
                if more:
                    time.sleep(min(wait, 60))
                continue
            if r.status_code >= 500:   # Telegram-side hiccup (502 Bad Gateway etc.): try again
                last = data
                log.warning("telegram %s: HTTP %s (attempt %d)", method, r.status_code, attempt + 1)
                if more:
                    time.sleep(2 * (attempt + 1))
                continue
            if not data.get("ok"):
                log.warning("telegram %s error: %s", method, data.get("description"))
            return data
        log.warning("telegram %s gave up: %s", method, last.get("description"))
        return last

    def send(self, text: str, chat_id: Optional[str] = None, preview: bool = False, silent: bool = False) -> bool:
        """Send `text` (HTML). Returns True when every chunk was accepted by Telegram for at least one target
        (a wrong extra id in TELEGRAM_CHAT_ID must not make every alert repeat for the good one).
        Returns False when nothing could be delivered, including when the bot has no token or no chat."""
        targets = [chat_id] if chat_id else self.chat_ids
        if not self.token or not targets:
            print("---- [telegram disabled] ----\n" + text + "\n-----------------------------")
            return False
        delivered_all = True
        for chunk in split_message(text):
            chunk_ok = False
            for cid in targets:
                res = self.api("sendMessage", chat_id=cid, text=chunk, parse_mode="HTML",
                               link_preview_options={"is_disabled": not preview}, disable_notification=silent)
                if not res.get("ok") and "parse" in str(res.get("description", "")).lower():
                    # fall back to plain text if our HTML was rejected
                    log.warning("telegram rejected our HTML (%s); sending plain text", res.get("description"))
                    res = self.api("sendMessage", chat_id=cid, text=_strip_tags(chunk),
                                   link_preview_options={"is_disabled": True}, disable_notification=silent)
                if res.get("ok"):
                    chunk_ok = True
                    self.sent += 1
            delivered_all = delivered_all and chunk_ok
        return delivered_all


def _last_sunday(year: int, month: int) -> _dt.date:
    d = _dt.date(year, month + 1, 1) - _dt.timedelta(days=1) if month < 12 else _dt.date(year, 12, 31)
    return d - _dt.timedelta(days=(d.weekday() + 1) % 7)


def _eu_local_now(base_offset_h: int, utc_now: Optional[_dt.datetime] = None) -> _dt.datetime:
    """Local time for an EU zone without a tz database: DST from the last Sunday of March 01:00 UTC
    to the last Sunday of October 01:00 UTC."""
    utc = utc_now or _dt.datetime.now(_dt.timezone.utc)
    start = _dt.datetime.combine(_last_sunday(utc.year, 3), _dt.time(1), _dt.timezone.utc)
    end = _dt.datetime.combine(_last_sunday(utc.year, 10), _dt.time(1), _dt.timezone.utc)
    offset = base_offset_h + (1 if start <= utc < end else 0)
    return utc.astimezone(_dt.timezone(_dt.timedelta(hours=offset)))


def local_now(tz: str = "Europe/Bucharest") -> _dt.datetime:
    """Current time in `tz`. Works without a tz database (Windows Python without the tzdata package)."""
    try:
        from zoneinfo import ZoneInfo

        return _dt.datetime.now(ZoneInfo(tz))
    except Exception as e:  # ZoneInfoNotFoundError, ValueError, OSError, ImportError
        log.debug("time zone %s unavailable (%s); using the built-in Bucharest rule", tz, e)
        return _eu_local_now(2)   # Romania: UTC+2, UTC+3 in summer


def in_quiet_hours(spec: Optional[str], tz: str = "Europe/Bucharest", now_dt=None) -> bool:
    """spec like "23-8" or "23:30-07:00" (local time). Wraps around midnight. Never raises."""
    if not spec:
        return False
    try:
        a, b = [x.strip() for x in str(spec).split("-", 1)]

        def mins(x: str) -> int:
            h, _, m = x.partition(":")
            return int(h) * 60 + int(m or 0)

        start, end = mins(a), mins(b)
        now_dt = now_dt or local_now(tz)
        cur = now_dt.hour * 60 + now_dt.minute
    except Exception:
        return False
    return start <= cur < end if start <= end else (cur >= start or cur < end)


# Tags Telegram's HTML parse mode understands. The plain-text fallback removes only these, so literal text
# such as "<3200" (a price criterion) survives.
TG_TAGS = ("a", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler", "tg-emoji",
           "code", "pre", "blockquote")
_TG_TAG_RE = re.compile(r"</?(?:%s)(?:\s[^<>]*)?>" % "|".join(re.escape(t) for t in TG_TAGS), re.I)


def _strip_tags(s: str) -> str:
    return html.unescape(_TG_TAG_RE.sub("", s))


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for block in text.split("\n\n"):
        candidate = (cur + "\n\n" + block) if cur else block
        if len(candidate) <= limit:
            cur = candidate
            continue
        if cur:
            parts.append(cur)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        cur = block
    if cur:
        parts.append(cur)
    return parts


def store_label(store_key: str, store_names: dict[str, str], seller: Optional[str] = None,
                shop_count: Optional[int] = None) -> str:
    name = store_names.get(store_key, store_key)
    extra = []
    if seller:
        extra.append(seller)
    if shop_count:
        extra.append(f"{shop_count} маг.")
    return f"{name} ({', '.join(extra)})" if extra else name


def format_offer_line(a: Alert, store_names: dict[str, str]) -> str:
    o = a.offer
    price = f"<b>{fmt_price(o.price)} лей</b>"
    if o.old_price and o.old_price > o.price:
        price += f" <s>{fmt_price(o.old_price)}</s>"
    if a.discount:
        price += f" (−{a.discount:g}%)"
    head = f"{KIND_ICON.get(a.kind, '•')} {price} — {esc(store_label(o.store, store_names, o.seller, o.shop_count))}"
    lines = [head, f'<a href="{esc_attr(o.url)}">{esc(o.title[:150])}</a>']
    notes = []
    if a.kind == "drop" and a.prev_price:
        notes.append(f"было {fmt_price(a.prev_price)}")
    if a.kind == "back":
        notes.append("снова в наличии")
    if a.kind == "rematch":
        notes.append("снова подходит под критерии")
    if a.min_seen and a.min_seen < o.price - 0.5:
        notes.append(f"мин. замечено {fmt_price(a.min_seen)}")
    if o.in_stock is None and o.store not in ("compari", "shopmania", "pricero"):
        notes.append("наличие неизвестно")
    if "resigilat" in o.title.lower():
        notes.append("resigilat (вскрытая упаковка)")
    if notes:
        lines.append("<i>" + esc(" · ".join(notes)) + "</i>")
    return "\n".join(lines)


def format_alerts(watch_name: str, alerts: list[Alert], store_names: dict[str, str],
                  digest: bool = False, total_matches: int = 0) -> str:
    if digest:
        head = f"🔎 <b>{esc(watch_name)}</b>: найдено {total_matches} подходящих предложений, самые дешёвые:"
    else:
        kinds = {a.kind for a in alerts}
        if kinds == {"drop"}:
            head = f"📉 <b>{esc(watch_name)}</b>: цена снизилась"
        else:
            head = f"🔥 <b>{esc(watch_name)}</b>: {len(alerts)} нов. предложени{'е' if len(alerts) == 1 else 'я'}"
    body = "\n\n".join(format_offer_line(a, store_names) for a in alerts)
    return f"{head}\n\n{body}"
