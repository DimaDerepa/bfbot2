"""Persistent JSON state: offers seen per watch, price history, Telegram offset, store health."""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Offer, Watch

STATE_VERSION = 1


def now() -> int:
    return int(time.time())


@dataclass
class Alert:
    kind: str                 # "new" | "drop" | "back" | "rematch"
    watch: Watch
    offer: Offer
    prev_price: Optional[float] = None
    min_seen: Optional[float] = None
    discount: Optional[float] = None


@dataclass
class State:
    data: dict = field(default_factory=dict)
    path: Optional[str] = None

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, path: str) -> "State":
        data: dict = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f) or {}
            except (ValueError, OSError):
                data = {}
        st = cls(data=data, path=path)
        st._defaults()
        return st

    def _defaults(self) -> None:
        d = self.data
        d.setdefault("version", STATE_VERSION)
        d.setdefault("telegram", {}).setdefault("offset", 0)
        d["telegram"].setdefault("chat_id", None)
        d.setdefault("watches", [])            # watches added via Telegram (dicts)
        d.setdefault("disabled", [])           # ids of config watches disabled via Telegram
        d.setdefault("initialized", [])        # watch ids that already had their first digest
        d.setdefault("offers", {})             # "<watch_id>|<offer_key>" -> record
        d.setdefault("stores", {})             # store health
        d.setdefault("runs", 0)
        d.setdefault("last_run", None)

    def save(self, path: Optional[str] = None) -> None:
        path = path or self.path
        if not path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=os.path.dirname(os.path.abspath(path)))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)

    # ------------------------------------------------------------------ helpers
    @property
    def offers(self) -> dict:
        return self.data["offers"]

    @staticmethod
    def rec_key(watch: Watch, offer: Offer) -> str:
        return f"{watch.id}|{offer.key}"

    def reference_price(self, watch: Watch, offer: Offer) -> Optional[float]:
        rec = self.offers.get(self.rec_key(watch, offer))
        if not rec:
            return None
        hist = [p for _, p in rec.get("h", [])]
        return max(hist) if hist else None

    def is_initialized(self, watch: Watch) -> bool:
        return watch.id in self.data["initialized"]

    def mark_initialized(self, watch: Watch) -> None:
        if watch.id not in self.data["initialized"]:
            self.data["initialized"].append(watch.id)

    # ------------------------------------------------------------------ core logic
    def observe(self, watch: Watch, offer: Offer, matched: bool, drop_pct: float,
                discount: Optional[float] = None) -> Optional[Alert]:
        """Record an offer whose TITLE matched the watch; return an Alert if the user should hear about it."""
        k = self.rec_key(watch, offer)
        ts = now()
        rec = self.offers.get(k)
        alert: Optional[Alert] = None
        if rec is None:
            rec = {
                "t": offer.title[:200], "u": offer.url, "s": offer.store, "p": offer.price,
                "op": offer.old_price, "st": offer.in_stock, "fs": ts, "ls": ts, "min": offer.price,
                "h": [[ts, offer.price]], "np": None, "m": matched,
            }
            self.offers[k] = rec
            if matched:
                alert = Alert("new", watch, offer, None, offer.price, discount)
        else:
            prev_price = rec.get("p")
            prev_stock = rec.get("st")
            prev_matched = rec.get("m", False)
            notified = rec.get("np")
            if matched:
                if notified is None:
                    alert = Alert("new", watch, offer, prev_price, rec.get("min"), discount)
                elif not prev_matched:
                    alert = Alert("rematch", watch, offer, notified, rec.get("min"), discount)
                elif offer.price <= notified * (1 - drop_pct / 100.0):
                    alert = Alert("drop", watch, offer, notified, rec.get("min"), discount)
                elif prev_stock is False and offer.in_stock is True:
                    alert = Alert("back", watch, offer, prev_price, rec.get("min"), discount)
            if prev_price != offer.price:
                rec.setdefault("h", []).append([ts, offer.price])
                rec["h"] = rec["h"][-40:]
            rec.update({"t": offer.title[:200], "u": offer.url, "p": offer.price, "op": offer.old_price,
                        "st": offer.in_stock, "ls": ts, "m": matched})
            rec["min"] = min(rec.get("min") or offer.price, offer.price)
        if alert is not None:
            alert.min_seen = min(rec.get("min") or offer.price, offer.price)
        return alert

    def mark_notified(self, alert: Alert) -> None:
        rec = self.offers.get(self.rec_key(alert.watch, alert.offer))
        if rec is not None:
            rec["np"] = alert.offer.price

    def prune(self, history_days: int, active_watch_ids: set[str]) -> int:
        cutoff = now() - history_days * 86400
        drop = [k for k, r in self.offers.items()
                if r.get("ls", 0) < cutoff or k.split("|", 1)[0] not in active_watch_ids]
        for k in drop:
            del self.offers[k]
        self.data["initialized"] = [w for w in self.data["initialized"] if w in active_watch_ids]
        return len(drop)

    def best_offers(self, watch: Watch, limit: int = 10, max_age_h: float = 6) -> list[dict]:
        cutoff = now() - max_age_h * 3600
        prefix = watch.id + "|"
        rows = [r for k, r in self.offers.items() if k.startswith(prefix) and r.get("m") and r.get("ls", 0) >= cutoff]
        rows.sort(key=lambda r: r.get("p") or 1e12)
        return rows[:limit]

    # ------------------------------------------------------------------ store health
    def store_result(self, store: str, ok: bool, error: str = "", count: int = 0) -> dict:
        h = self.data["stores"].setdefault(store, {"ok": 0, "fail": 0, "cf": 0, "alerted": False})
        if ok:
            h["ok"] += 1
            h["cf"] = 0
            h["last_ok"] = now()
            h["last_count"] = count
        else:
            h["fail"] += 1
            h["cf"] = h.get("cf", 0) + 1
            h["last_error"] = error[:300]
            h["last_fail"] = now()
        return h

    # ------------------------------------------------------------------ telegram-managed watches
    def tg_watches(self) -> list[dict]:
        return self.data["watches"]

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur
