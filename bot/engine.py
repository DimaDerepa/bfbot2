"""One monitoring cycle: query stores, match offers against watches, decide and send alerts."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from .categorize import infer_categories
from .config import Config, watch_from_dict
from .matcher import evaluate
from .models import Offer, Watch
from .net import BlockedError, FetchError, Http, proxy_for
from .notify import Telegram, esc, format_alerts, in_quiet_hours
from .state import Alert, State
from .stores import Store, registry, store_for_url, web_store_for

log = logging.getLogger("bot.engine")


@dataclass
class StoreRun:
    key: str
    results: dict[str, list[Offer]] = field(default_factory=dict)    # query -> offers
    products: dict[str, Optional[Offer]] = field(default_factory=dict)  # url -> offer
    errors: dict[str, str] = field(default_factory=dict)             # query/url -> error
    blocked: bool = False
    seconds: float = 0.0
    requests: int = 0


class Engine:
    def __init__(self, cfg: Config, state: State, tg: Telegram, *, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.settings = cfg.settings
        self.state = state
        self.tg = tg
        self.dry_run = dry_run
        self.reg = registry(self.settings.shops)
        self.store_names = {k: c.name for k, c in self.reg.items()}

    # ------------------------------------------------------------------ watches
    def all_watches(self) -> list[Watch]:
        """Config watches (with Telegram overrides applied) followed by Telegram-added watches."""
        out: list[Watch] = []
        overrides = self.state.data.get("overrides", {})
        disabled = set(self.state.data.get("disabled", []))
        for w in self.cfg.watches:
            if w.id in overrides:
                try:
                    w = watch_from_dict({**_watch_to_spec(w), **overrides[w.id]}, origin="config")
                except (ValueError, TypeError) as e:
                    log.warning("bad override for %s: %s", w.id, e)
            if w.id in disabled:
                w.enabled = False
            out.append(w)
        taken = {w.id for w in out}
        for d in self.state.tg_watches():
            try:
                w = watch_from_dict({k: v for k, v in d.items() if k != "origin"}, origin="telegram")
            except (ValueError, TypeError) as e:
                log.warning("bad telegram watch %s: %s", d, e)
                continue
            if w.id in taken:
                continue
            taken.add(w.id)
            out.append(w)
        return out

    def active_watches(self) -> list[Watch]:
        return [w for w in self.all_watches() if w.enabled]

    def stores_for(self, watch: Watch) -> list[str]:
        if watch.stores:
            keys = [k for k in watch.stores if k in self.reg]
            unknown = [k for k in watch.stores if k not in self.reg]
            if unknown:
                log.warning("watch %s: unknown stores %s", watch.id, unknown)
        else:
            default = self.settings.default_stores
            if isinstance(default, (list, tuple)):
                keys = [k for k in default if k in self.reg]
            else:
                keys = [k for k, c in self.reg.items() if c.enabled_by_default]
            keys = [k for k in keys if k not in set(self.settings.disabled_stores)]
            cats = watch.categories or self.settings.default_categories
            if not cats and watch.query:
                cats = sorted(infer_categories(watch.query))  # e.g. "rtx 5070" -> pc: skip fashion/beauty shops
            if cats:
                cats_set = set(cats)
                keys = [k for k in keys if self.reg[k].aggregator or "general" in self.reg[k].categories
                        or cats_set & set(self.reg[k].categories)]
        skip = set(watch.skip_stores)
        return [k for k in keys if k not in skip]

    # ------------------------------------------------------------------ fetching
    def _make_store(self, cls: type[Store]) -> Store:
        http = Http(timeout=self.settings.timeout, delay=max(self.settings.request_delay, cls.min_delay),
                    proxy=proxy_for(cls.key), extra_headers=cls.extra_headers or None,
                    browser_key=cls.key)
        return cls(http, max_pages=self.settings.max_pages)

    def _run_store(self, cls: type[Store], queries: list[str], urls: list[str]) -> StoreRun:
        run = StoreRun(cls.key)
        t0 = time.monotonic()
        store = self._make_store(cls)
        for q in queries:
            if run.blocked:
                run.errors[q] = "skipped (store blocked)"
                continue
            try:
                run.results[q] = store.search(q)
            except BlockedError as e:
                run.blocked = True
                run.errors[q] = f"BLOCKED: {e}"
            except FetchError as e:
                run.errors[q] = str(e)
            except Exception as e:  # parser bug etc.
                log.exception("store %s query %r crashed", cls.key, q)
                run.errors[q] = f"{type(e).__name__}: {e}"
        for u in urls:
            try:
                run.products[u] = store.product(u)
                if run.products[u] is None:
                    run.errors[u] = "цена на странице не найдена"
            except Exception as e:
                run.errors[u] = f"{type(e).__name__}: {e}"
        run.seconds = time.monotonic() - t0
        run.requests = store.http.requests_made
        return run

    def fetch_all(self, watches: list[Watch]) -> dict[str, StoreRun]:
        queries: dict[str, set[str]] = defaultdict(set)
        urls: dict[str, set[str]] = defaultdict(set)
        classes: dict[str, type[Store]] = {}
        for w in watches:
            if w.query:
                for k in self.stores_for(w):
                    queries[k].add(w.query)
                    classes[k] = self.reg[k]
            for u in w.urls:
                cls = store_for_url(u, self.reg) or web_store_for(u)
                urls[cls.key].add(u)
                classes[cls.key] = cls
        run_no = int(self.state.data.get("runs", 0))
        for k, every in (self.settings.store_every or {}).items():
            try:
                every = int(every)
            except (TypeError, ValueError):
                continue
            if every > 1 and k in classes and run_no % every != 0 and not urls.get(k):
                classes.pop(k)  # rate-limited store (e.g. behind a paid proxy): skip this run
        runs: dict[str, StoreRun] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.settings.workers)) as ex:
            futs = {ex.submit(self._run_store, cls, sorted(queries.get(k, ())), sorted(urls.get(k, ()))): k
                    for k, cls in classes.items()}
            for f in as_completed(futs):
                k = futs[f]
                try:
                    runs[k] = f.result()
                except Exception as e:  # pragma: no cover
                    runs[k] = StoreRun(k, errors={"*": str(e)})
        return runs

    # ------------------------------------------------------------------ cycle
    def run_cycle(self) -> dict:
        t0 = time.monotonic()
        watches = self.active_watches()
        runs = self.fetch_all(watches) if watches else {}
        drop_default = float(self.settings.notify_drop_pct)
        summary = {"watches": len(watches), "alerts": 0, "matches": 0, "stores": {}}

        for w in watches:
            store_keys = self.stores_for(w) if w.query else []
            any_store_ok = False
            matched_offers: list[tuple[Offer, Optional[float]]] = []
            alerts: list[Alert] = []
            first = not self.state.is_initialized(w)
            candidates: list[tuple[Offer, bool]] = []  # (offer, check_title)
            for k in store_keys:
                r = runs.get(k)
                if r and w.query in r.results:
                    any_store_ok = True
                    candidates.extend((o, True) for o in r.results[w.query])
            for u in w.urls:
                for r in runs.values():
                    if u in r.products:
                        if r.products[u] is not None:
                            any_store_ok = True
                            candidates.append((r.products[u], False))
            for o, check_title in candidates:
                ref = self.state.reference_price(w, o)
                res = evaluate(w, o, ref, check_title=check_title)
                if not res.title_ok:
                    continue
                if res.ok:
                    matched_offers.append((o, res.discount))
                a = self.state.observe(w, o, res.ok, w.drop_pct if w.drop_pct is not None else drop_default, res.discount)
                if a:
                    alerts.append(a)
            summary["matches"] += len(matched_offers)

            if first:
                if not any_store_ok:
                    continue  # nothing fetched yet; keep the watch un-initialised
                self.state.mark_initialized(w)
                top_n = max(1, int(self.settings.first_run_digest))
                new_alerts = sorted([a for a in alerts if a.kind in ("new", "rematch")], key=lambda a: a.offer.price)
                for a in new_alerts:
                    self.state.mark_notified(a)  # do not re-announce everything one by one later
                if new_alerts:
                    msg = format_alerts(w.name, new_alerts[:top_n], self.store_names, digest=True,
                                        total_matches=len(new_alerts))
                else:
                    msg = (f"👀 <b>{esc(w.name)}</b>: сейчас подходящих предложений нет. "
                           f"Слежу ({len(store_keys) or len(w.urls)} источн.) и напишу, как только появятся.")
                self._send(msg)
                summary["alerts"] += min(len(new_alerts), top_n)
                continue

            if alerts:
                # cheapest first; everything is marked as notified so nothing trickles in later runs
                alerts.sort(key=lambda a: a.offer.price)
                limit = max(1, int(self.settings.max_alerts_per_watch))
                send, rest = alerts[:limit], alerts[limit:]
                msg = format_alerts(w.name, send, self.store_names)
                if rest:
                    msg += f"\n\n…и ещё {len(rest)} подходящих (дороже). Список: /top {w.id}"
                self._send(msg)
                for a in alerts:
                    self.state.mark_notified(a)
                summary["alerts"] += len(send)

        self._store_health(runs)
        for k, r in runs.items():
            summary["stores"][k] = {
                "ok": not r.errors or bool(r.results) or any(r.products.values()),
                "offers": sum(len(v) for v in r.results.values()),
                "errors": r.errors, "sec": round(r.seconds, 1), "requests": r.requests,
            }
        self.state.prune(self.settings.history_days, {w.id for w in self.all_watches()})
        self.state.data["runs"] = self.state.data.get("runs", 0) + 1
        self.state.data["last_run"] = int(time.time())
        self.state.data["last_summary"] = {
            "watches": summary["watches"], "alerts": summary["alerts"], "matches": summary["matches"],
            "seconds": round(time.monotonic() - t0, 1),
        }
        summary["seconds"] = round(time.monotonic() - t0, 1)
        return summary

    def _store_health(self, runs: dict[str, StoreRun]) -> None:
        threshold = max(1, int(self.settings.store_fail_alert_after))
        down: list[str] = []
        up: list[str] = []
        any_blocked = False
        for k, r in runs.items():
            if k.startswith("web:") and not r.results:
                continue
            failed = bool(r.errors) and not r.results and not any(r.products.values())
            err = "; ".join(sorted(set(r.errors.values())))[:300]
            h = self.state.store_result(k, not failed, err, sum(len(v) for v in r.results.values()))
            name = self.store_names.get(k, k)
            if failed and h["cf"] >= threshold and not h.get("alerted"):
                h["alerted"] = True
                any_blocked = any_blocked or r.blocked
                down.append(f"• <b>{esc(name)}</b> — {h['cf']} проверок подряд: <code>{esc(err[:120])}</code>")
            elif not failed and h.get("alerted"):
                h["alerted"] = False
                up.append(esc(name))
        if down:
            hint = ("\nПохоже, сайт блокирует запросы из облака GitHub. Цены этого магазина часто видны через "
                    "агрегаторы (compari.ro); иначе поможет прокси или запуск дома — см. README." if any_blocked else "")
            self._send("⚠️ Магазины не отвечают:\n" + "\n".join(down) + hint)
        if up:
            self._send("✅ Снова работают: " + ", ".join(up))

    def _send(self, text: str) -> None:
        if self.dry_run:
            print("---- [dry-run message] ----\n" + text + "\n---------------------------")
            return
        chat = self.state.get("telegram", "chat_id")
        silent = in_quiet_hours(self.settings.quiet_hours)
        if self.tg.chat_ids:
            self.tg.send(text, silent=silent)
        elif chat:
            self.tg.send(text, chat_id=str(chat), silent=silent)
        else:
            print("---- [no chat bound yet] ----\n" + text)


def _watch_to_spec(w: Watch) -> dict:
    d = w.to_dict()
    d.pop("origin", None)
    return d
