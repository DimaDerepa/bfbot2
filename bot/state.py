"""Persistent JSON state: offers seen per watch, price history, Telegram offset, store health."""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Offer, Watch

log = logging.getLogger("bot.state")

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
    _saved: Optional[str] = field(default=None, repr=False, compare=False)  # what is on disk (save_if_changed)

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, path: str) -> "State":
        """Load the state file. A missing file means a fresh start.

        A file that cannot be READ (permissions, I/O error) raises: starting empty would make the next save
        overwrite the good state. A file that is not valid JSON is kept as <path>.corrupt-<time> and the bot
        starts fresh, so a human can still recover it.
        """
        data: dict = {}
        saved: Optional[str] = None
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:   # OSError propagates on purpose
                raw = f.read()
            try:
                data = json.loads(raw) if raw.strip() else {}
                if not isinstance(data, dict):
                    raise ValueError(f"top level is {type(data).__name__}, not an object")
                saved = raw
            except ValueError as e:
                backup = f"{path}.corrupt-{now()}"
                try:
                    shutil.copyfile(path, backup)
                except OSError:  # pragma: no cover
                    backup = "(backup failed)"
                log.error("state file %s is not valid JSON (%s); starting fresh, old file kept as %s", path, e, backup)
                data = {}
        st = cls(data=data, path=path)
        st._defaults()
        st._saved = saved and st._dump()
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

    def _dump(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))

    def save(self, path: Optional[str] = None) -> None:
        path = path or self.path
        if not path:
            return
        text = self._dump()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=os.path.dirname(os.path.abspath(path)))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        if path == self.path:
            self._saved = text

    def save_if_changed(self) -> bool:
        """Write the file only when the data differs from what was last loaded/saved (daemon: spare the SD card)."""
        if not self.path or self._dump() == self._saved:
            return False
        self.save()
        return True

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
            pending = rec.get("pa")            # "rematch"/"back" alert that could not be delivered last time
            try:
                drop_pct = max(float(drop_pct), 0.0)
            except (TypeError, ValueError):
                drop_pct = 0.0
            if matched:
                if notified is None:
                    alert = Alert("new", watch, offer, prev_price, rec.get("min"), discount)
                elif not prev_matched:
                    alert = Alert("rematch", watch, offer, notified, rec.get("min"), discount)
                elif offer.price < notified and offer.price <= notified * (1 - drop_pct / 100.0):
                    alert = Alert("drop", watch, offer, notified, rec.get("min"), discount)
                elif prev_stock is False and offer.in_stock is True:
                    alert = Alert("back", watch, offer, prev_price, rec.get("min"), discount)
                elif pending == "rematch":
                    alert = Alert("rematch", watch, offer, notified, rec.get("min"), discount)
                elif pending == "back" and offer.in_stock is not False:
                    alert = Alert("back", watch, offer, prev_price, rec.get("min"), discount)
            if not matched or (pending == "back" and offer.in_stock is False):
                rec.pop("pa", None)
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
        """Call only after the alert was actually delivered."""
        rec = self.offers.get(self.rec_key(alert.watch, alert.offer))
        if rec is not None:
            rec["np"] = alert.offer.price
            rec.pop("pa", None)

    def mark_pending(self, alert: Alert) -> None:
        """The alert could not be delivered: make the next observe() produce it again.

        "new" and "drop" alerts repeat by themselves (they depend on the notified price, which stays unchanged);
        "rematch" and "back" depend on the previous matched/stock flags that observe() has already updated.
        """
        rec = self.offers.get(self.rec_key(alert.watch, alert.offer))
        if rec is not None and alert.kind in ("rematch", "back"):
            rec["pa"] = alert.kind

    def prune(self, history_days: int, active_watch_ids: set[str]) -> int:
        cutoff = now() - history_days * 86400
        drop = [k for k, r in self.offers.items()
                if r.get("ls", 0) < cutoff or k.split("|", 1)[0] not in active_watch_ids]
        for k in drop:
            del self.offers[k]
        self.data["initialized"] = [w for w in self.data["initialized"] if w in active_watch_ids]
        uf = self.data.get("url_fail")
        if isinstance(uf, dict):
            for k in [k for k in uf if k.split("|", 1)[0] not in active_watch_ids]:
                del uf[k]
        return len(drop)

    # ------------------------------------------------------------------ per-watch bookkeeping
    def _watch_keys(self, watch_id: str) -> tuple[list[str], list[str]]:
        prefix = watch_id + "|"
        offers = [k for k in self.offers if k.startswith(prefix)]
        uf = self.data.get("url_fail") or {}
        urls = [k for k in uf if k.startswith(prefix)]
        return offers, urls

    def forget_watch(self, watch_id: str) -> None:
        """Drop everything stored under a watch id (offers, first-run flag, URL failures).

        Used when an id is (re)assigned to a NEW watch, so it does not inherit a deleted watch's history."""
        offers, urls = self._watch_keys(watch_id)
        for k in offers:
            del self.offers[k]
        for k in urls:
            del self.data["url_fail"][k]
        self.data["initialized"] = [i for i in self.data["initialized"] if i != watch_id]

    def rename_watch(self, old_id: str, new_id: str) -> None:
        """Move all state of watch `old_id` to `new_id` (id collision between config.yaml and Telegram)."""
        if old_id == new_id:
            return
        self.forget_watch(new_id)
        offers, urls = self._watch_keys(old_id)
        for k in offers:
            self.offers[new_id + k[len(old_id):]] = self.offers.pop(k)
        uf = self.data.get("url_fail") or {}
        for k in urls:
            uf[new_id + k[len(old_id):]] = uf.pop(k)
        init = self.data["initialized"]
        if old_id in init:
            self.data["initialized"] = [i for i in init if i != old_id] + [new_id]

    def url_result(self, watch_id: str, url: str, ok: bool, error: str = "") -> dict:
        """Consecutive-failure bookkeeping for one URL of a URL watch."""
        uf = self.data.setdefault("url_fail", {})
        k = f"{watch_id}|{url}"
        h = uf.setdefault(k, {"cf": 0, "alerted": False})
        if ok:
            h["cf"] = 0
            h["last_ok"] = now()
        else:
            h["cf"] = h.get("cf", 0) + 1
            h["last_error"] = (error or "")[:300]
            h["last_fail"] = now()
        return h

    def best_offers(self, watch: Watch, limit: int = 10, max_age_h: float = 6) -> list[dict]:
        cutoff = now() - max_age_h * 3600
        prefix = watch.id + "|"
        pools = [self.offers, self.data.get("home_offers") or {}]   # home_offers: read-only copy from the home runner
        rows = [r for pool in pools for k, r in pool.items()
                if k.startswith(prefix) and r.get("m") and r.get("ls", 0) >= cutoff]
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
