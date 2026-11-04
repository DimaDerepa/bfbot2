"""Telegram delivery + message formatting (Russian UI)."""
from __future__ import annotations

import html
import logging
import os
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
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in range(4):
            try:
                r = self._s.post(url, json=params, timeout=_http_timeout)
            except requests.RequestException as e:
                log.warning("telegram %s failed: %s", method, e)
                time.sleep(2 * (attempt + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                data = {"ok": False, "description": r.text[:200]}
            if r.status_code == 429:
                wait = (data.get("parameters") or {}).get("retry_after", 5)
                time.sleep(min(float(wait), 60))
                continue
            if not data.get("ok"):
                log.warning("telegram %s error: %s", method, data.get("description"))
            return data
        return {"ok": False, "description": "network error"}

    def send(self, text: str, chat_id: Optional[str] = None, preview: bool = False) -> None:
        targets = [chat_id] if chat_id else self.chat_ids
        if not self.token or not targets:
            print("---- [telegram disabled] ----\n" + text + "\n-----------------------------")
            return
        for chunk in split_message(text):
            for cid in targets:
                res = self.api("sendMessage", chat_id=cid, text=chunk, parse_mode="HTML",
                               link_preview_options={"is_disabled": not preview})
                if not res.get("ok") and "parse" in str(res.get("description", "")).lower():
                    # fall back to plain text if our HTML was rejected
                    self.api("sendMessage", chat_id=cid, text=_strip_tags(chunk),
                             link_preview_options={"is_disabled": True})
                self.sent += 1


def _strip_tags(s: str) -> str:
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", s))


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
    lines = [head, f'<a href="{esc(o.url)}">{esc(o.title[:150])}</a>']
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
