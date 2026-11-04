"""One monitoring cycle: query stores, match offers against watches, decide and send alerts."""
from __future__ import annotations

import dataclasses
import inspect
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional

from .categorize import infer_categories
from .config import Config, watch_from_dict
from .matcher import evaluate
from .models import Offer, Watch
from .net import BlockedError, FetchError, Http, proxy_for
from .notify import Telegram, esc, format_alerts, in_quiet_hours
from .state import Alert, State
from .stores import Store, registry, store_for_url, web_store_for

log = logging.getLogger("bot.engine")

STORE_TIME_BUDGET = 240.0    # s per store and run; queries/URLs left after that are skipped until the next run
FETCH_TIME_BUDGET = 900.0    # s for the whole fetch phase (the GitHub job is killed after 25 min)
WARN_COOLDOWN = 24 * 3600    # s between two "⚠️ не отвечает" messages for the same store / link

SKIP_BUDGET = "skipped (time budget)"


def _setting(settings: Any, name: str, default: Any, conv=float) -> Any:
    """Read a numeric setting; a typo in config.yaml must not crash every cycle."""
    try:
        v = getattr(settings, name, default)
        return conv(default if v is None else v)
    except (TypeError, ValueError):
        log.warning("setting %s=%r is not a number; using %r", name, getattr(settings, name, None), default)
        return default


def _http_params() -> set[str]:
    try:
        return set(inspect.signature(Http.__init__).parameters)
    except (TypeError, ValueError):  # pragma: no cover
        return set()


def http_for_store(cls: type[Store], settings: Any) -> Http:
    """The Http client a store adapter runs with (shared by the engine and the CLI)."""
    min_delay = float(getattr(cls, "min_delay", 0.0) or 0.0)
    kw: dict[str, Any] = dict(timeout=_setting(settings, "timeout", 25.0),
                              delay=max(_setting(settings, "request_delay", 1.5), min_delay),
                              proxy=proxy_for(cls.key), extra_headers=getattr(cls, "extra_headers", None) or None,
                              browser_key=cls.key)
    params = _http_params()
    impersonate = getattr(cls, "impersonate", None)
    if impersonate and "impersonate" in params:
        kw["impersonate"] = impersonate            # per-store TLS fingerprint (e.g. a pinned "chrome124")
    if "backoff" in params and min_delay > 0:
        kw["backoff"] = min_delay                  # slow stores also wait longer before retrying a 429/5xx
    return Http(**kw)


@dataclass
class StoreRun:
    key: str
    results: dict[str, list[Offer]] = field(default_factory=dict)    # query -> offers
    products: dict[str, Optional[Offer]] = field(default_factory=dict)  # url -> offer
    errors: dict[str, str] = field(default_factory=dict)             # query/url -> error
    blocked: bool = False
    down: bool = False                 # network-level failure (timeout / connection refused): rest skipped
    skipped: set[str] = field(default_factory=set)   # queries/urls not tried because the time budget ran out
    cause: str = ""                    # the error that made the rest of the store's work be skipped
    seconds: float = 0.0
    requests: int = 0
    queries: list[str] = field(default_factory=list)   # the searches this run was given (the rest are URLs)

    @property
    def attempted(self) -> bool:
        """Something was actually tried (a run where everything was skipped tells nothing about the store)."""
        return bool(self.results) or bool(self.products) or any(k not in self.skipped for k in self.errors)

    @property
    def searched(self) -> bool:
        """At least one search query was actually tried (not skipped by the time budget)."""
        return any(q not in self.skipped for q in self.queries)

    @property
    def store_failed(self) -> bool:
        """The shop as a whole failed (blocked / not responding) and nothing came back: one store-level problem,
        reported once by _store_health rather than once per product link."""
        return (self.blocked or self.down) and not self.results and not any(self.products.values())


class Engine:
    store_budget = STORE_TIME_BUDGET
    fetch_budget = FETCH_TIME_BUDGET
    _fetch_deadline: Optional[float] = None     # set by fetch_all for the duration of the fetch phase

    def __init__(self, cfg: Config, state: State, tg: Telegram, *, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.settings = cfg.settings
        for k, v in (state.data.get("settings_overrides") or {}).items():   # set from the Mini App
            if hasattr(self.settings, k):
                setattr(self.settings, k, v)
        self.state = state
        self.tg = tg
        self.dry_run = dry_run
        self.reg = registry(self.settings.shops)
        self.store_names = {k: c.name for k, c in self.reg.items()}
        # hybrid mode: the home runner checks only the shops that block the cloud, the cloud skips them
        home = set(self.settings.home_stores or [])
        mode = os.environ.get("BOT_MODE", "").lower()
        self.home_mode = mode == "home"
        if mode == "home":
            self.reg = {k: c for k, c in self.reg.items() if k in home}
        elif os.environ.get("BOT_HOME_ENABLED", "").lower() in ("1", "true", "yes"):
            self.reg = {k: c for k, c in self.reg.items() if k not in home}
        self._bad_urls: dict[str, str] = {}          # url -> error, for URLs that could not even be parsed
        self._query_pages: dict[str, int] = {}       # query -> result pages per shop (parameter searches)
        self._fetch_deadline: Optional[float] = None

    # ------------------------------------------------------------------ watches
    def all_watches(self) -> list[Watch]:
        """Config watches (with Telegram overrides applied) followed by Telegram-added watches."""
        out: list[Watch] = []
        overrides = self.state.data.get("overrides", {})
        disabled = set(self.state.data.get("disabled", []))
        for w in self.cfg.watches:
            if w.id in overrides:
                try:
                    w = _stored_watch({**_watch_to_spec(w), **overrides[w.id]}, origin="config")
                except (ValueError, TypeError) as e:
                    log.warning("bad override for %s: %s", w.id, e)
            if w.id in disabled:
                # a copy: the config object must stay enabled, or /pause then /resume in the same run (one Engine,
                # one Config) would leave it paused until the next run
                w = dataclasses.replace(w, enabled=False)
            out.append(w)
        parsed: list[tuple[dict, Watch]] = []
        for d in self.state.tg_watches():
            try:
                parsed.append((d, _stored_watch({k: v for k, v in d.items() if k != "origin"}, origin="telegram")))
            except (ValueError, TypeError) as e:
                log.warning("bad telegram watch %s: %s", d, e)
        config_ids = {w.id for w in out}
        reserved = config_ids | {w.id for _, w in parsed}
        taken = set(config_ids)
        for d, w in parsed:
            if w.id in taken:
                w = self._rename_colliding(d, w, reserved, from_config=w.id in config_ids)
            taken.add(w.id)
            out.append(w)
        return out

    def _rename_colliding(self, d: dict, w: Watch, reserved: set[str], *, from_config: bool) -> Watch:
        """Give a Telegram watch whose id is already used a new id (same "-2" rule as /add), stored in state.

        from_config: a config.yaml watch got the id of an older Telegram watch. The history stored under that
        id was made by the Telegram watch (/add never reuses a config id), so it moves along; the config watch
        starts fresh and gets its first-run digest. Both are monitored and reachable by /list, /del, /set."""
        old = w.id
        base, n = old, 2
        while w.id in reserved:
            w.id = f"{base}-{n}"
            n += 1
        reserved.add(w.id)
        d["id"] = w.id
        if from_config:
            home = getattr(self, "home_mode", False)
            # home runner: the rename lives only in its copy until the cloud runner renames the watch in the main
            # state, so it may meet the same collision on several runs; move the data only the first time
            if not (home and self.state.has_watch_data(w.id)):
                self.state.rename_watch(old, w.id)
            if not home:   # the cloud runner tells the user (once, not twice)
                notices = self.state.data.setdefault("notices", [])
                del notices[:-9]
                notices.append(
                    f"ℹ️ В config.yaml появился пункт с тем же именем, что и добавленный через Telegram "
                    f"«{esc(w.name)}». Слежу за обоими; у пункта из Telegram теперь id <code>{esc(w.id)}</code> "
                    f"(см. /list).")
        else:
            self.state.forget_watch(w.id)   # duplicate Telegram entry: never inherit another watch's history
        log.warning("watch id %r is used twice (%s); Telegram watch renamed to %r",
                    old, "config.yaml and Telegram" if from_config else "two Telegram watches", w.id)
        return w

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
        return cls(http_for_store(cls, self.settings), max_pages=max(1, _setting(self.settings, "max_pages", 1, int)))

    def _run_store(self, cls: type[Store], queries: list[str], urls: list[str]) -> StoreRun:
        run = StoreRun(cls.key, queries=list(queries))
        t0 = time.monotonic()
        deadline = t0 + float(self.store_budget)
        if self._fetch_deadline is not None:
            deadline = min(deadline, self._fetch_deadline)
        try:
            store = self._make_store(cls)
        except Exception as e:
            log.exception("store %s could not be created", cls.key)
            for k in list(queries) + list(urls):
                run.errors[k] = f"{type(e).__name__}: {e}"
            return run

        def skip(item: str) -> bool:
            if run.blocked:
                run.errors[item] = "skipped (store blocked)"
            elif run.down:
                run.errors[item] = "skipped (store not responding)"
            elif time.monotonic() >= deadline:
                run.errors[item] = SKIP_BUDGET
                run.skipped.add(item)
            else:
                return False
            return True

        def failed(item: str, e: Exception) -> None:
            if isinstance(e, BlockedError):
                run.blocked = True
                msg = f"BLOCKED: {e}"
            elif isinstance(e, FetchError):
                if e.status is None:     # timeout / connection error, after Http's own retries
                    run.down = True
                msg = str(e)
            else:  # parser bug etc.
                log.exception("store %s: %r crashed", cls.key, item)
                msg = f"{type(e).__name__}: {e}"
            run.errors[item] = msg
            if (run.blocked or run.down) and not run.cause:
                run.cause = msg

        for q in queries:
            if skip(q):
                continue
            try:
                # parameter searches ("telefon 12gb ram") look at several result pages
                store.max_pages = max(1, int(self._query_pages.get(q) or _setting(self.settings, "max_pages", 1, int)))
                run.results[q] = store.search(q)
            except Exception as e:
                failed(q, e)
        for u in urls:
            if skip(u):
                continue
            try:
                run.products[u] = store.product(u)
                if run.products[u] is None:
                    run.errors[u] = "цена на странице не найдена"
            except Exception as e:
                failed(u, e)
        if run.skipped:
            log.warning("store %s: time budget used up, skipped %d queries/links", cls.key, len(run.skipped))
        run.seconds = time.monotonic() - t0
        run.requests = getattr(store.http, "requests_made", 0)
        return run

    def fetch_all(self, watches: list[Watch]) -> dict[str, StoreRun]:
        queries: dict[str, set[str]] = defaultdict(set)
        urls: dict[str, set[str]] = defaultdict(set)
        classes: dict[str, type[Store]] = {}
        self._bad_urls = {}
        self._query_pages = {}
        for w in watches:
            if w.query:
                if w.pages:
                    for q in [w.query, *w.extra_queries]:
                        self._query_pages[q] = max(self._query_pages.get(q, 0), min(int(w.pages), 5))
                for k in self.stores_for(w):
                    for q in [w.query, *w.extra_queries]:
                        queries[k].add(q)
                    classes[k] = self.reg[k]
            for u in w.urls:
                try:
                    cls = store_for_url(u, self.reg) or web_store_for(u)
                except Exception as e:   # e.g. "https://[www.emag.ro/..." -> ValueError: Invalid IPv6 URL
                    log.warning("watch %s: bad url %r: %s", w.id, u, e)
                    self._bad_urls[u] = f"некорректная ссылка ({type(e).__name__}: {e})"
                    continue
                urls[cls.key].add(u)
                classes[cls.key] = cls
        try:
            run_no = int(self.state.data.get("runs", 0) or 0)
        except (TypeError, ValueError):
            run_no = 0
        for k, every in (self.settings.store_every or {}).items():
            try:
                every = int(every)
            except (TypeError, ValueError):
                continue
            if every > 1 and k in classes and run_no % every != 0 and not urls.get(k):
                classes.pop(k)  # rate-limited store (e.g. behind a paid proxy): skip this run
        runs: dict[str, StoreRun] = {}
        self._fetch_deadline = time.monotonic() + float(self.fetch_budget)
        try:
            with ThreadPoolExecutor(max_workers=max(1, int(_setting(self.settings, "workers", 8, int)))) as ex:
                futs = {ex.submit(self._run_store, cls, sorted(queries.get(k, ())), sorted(urls.get(k, ()))): k
                        for k, cls in classes.items()}
                for f in as_completed(futs):
                    k = futs[f]
                    try:
                        runs[k] = f.result()
                    except Exception as e:  # pragma: no cover
                        runs[k] = StoreRun(k, errors={"*": str(e)})
        finally:
            self._fetch_deadline = None
        return runs

    # ------------------------------------------------------------------ cycle
    def run_cycle(self) -> dict:
        t0 = time.monotonic()
        self._bad_urls = {}
        watches = self.active_watches()
        runs = self.fetch_all(watches) if watches else {}
        drop_default = max(0.0, _setting(self.settings, "notify_drop_pct", 2.0))
        top_n = max(1, _setting(self.settings, "first_run_digest", 8, int))
        limit = max(1, _setting(self.settings, "max_alerts_per_watch", 12, int))
        summary = {"watches": len(watches), "alerts": 0, "matches": 0, "stores": {}}
        self._send_notices()

        for w in watches:
            store_keys = self.stores_for(w) if w.query else []
            any_store_ok = False
            matched_offers: list[tuple[Offer, Optional[float]]] = []
            alerts: list[Alert] = []
            first = not self.state.is_initialized(w)
            candidates: list[tuple[Offer, bool]] = []  # (offer, check_title)
            seen_keys: set[str] = set()
            for k in store_keys:
                r = runs.get(k)
                for q in [w.query, *w.extra_queries]:
                    if r and q in r.results:
                        any_store_ok = True
                        for o in r.results[q]:
                            if o.key not in seen_keys:
                                seen_keys.add(o.key)
                                candidates.append((o, True))
            for u in w.urls:
                for r in runs.values():
                    if u in r.products:
                        if r.products[u] is not None:
                            any_store_ok = True
                            candidates.append((r.products[u], False))
            drop_pct = w.drop_pct if w.drop_pct is not None else drop_default
            judged = self._ai_judge(w, candidates)
            for o, check_title in candidates:
                ref = self.state.reference_price(w, o)
                v = judged.get(o.title) if check_title else None
                if v is None and check_title and w.ai and judged.get("__ai_on__"):
                    continue   # Claude-parsed watch: no verdict yet (API hiccup / per-run cap) -> next run
                if v is not None:
                    # Claude decided what the product is (type, model year, RAM); only price rules remain
                    res = evaluate(w, o, ref, check_title=False)
                    if not v["match"]:
                        continue
                else:
                    res = evaluate(w, o, ref, check_title=check_title)
                if not res.title_ok:
                    continue
                if res.ok:
                    matched_offers.append((o, res.discount))
                a = self.state.observe(w, o, res.ok, max(0.0, drop_pct), res.discount)
                if a:
                    alerts.append(a)
            summary["matches"] += len(matched_offers)

            if first:
                if not any_store_ok:
                    continue  # nothing fetched yet; keep the watch un-initialised
                new_alerts = sorted([a for a in alerts if a.kind in ("new", "rematch")], key=lambda a: a.offer.price)
                new_alerts = self._gate(w, new_alerts, keep=top_n)
                alerts = [a for a in alerts if a.kind not in ("new", "rematch") or a in new_alerts]
                if new_alerts:
                    msg = format_alerts(w.name, new_alerts[:top_n], self.store_names, digest=True,
                                        total_matches=len(new_alerts))
                else:
                    msg = (f"👀 <b>{esc(w.name)}</b>: сейчас подходящих предложений нет. "
                           f"Слежу ({len(store_keys) or len(w.urls)} источн.) и напишу, как только появятся.")
                if self._send(msg):
                    self.state.mark_initialized(w)
                    for a in new_alerts:
                        self.state.mark_notified(a)  # do not re-announce everything one by one later
                    summary["alerts"] += min(len(new_alerts), top_n)
                    sent_ids = {id(a) for a in new_alerts}
                    rest = [a for a in alerts if id(a) not in sent_ids]
                else:
                    rest = alerts  # not delivered (Telegram down, no chat bound yet): the next run builds it again
                for a in rest:
                    self.state.mark_pending(a)
                continue

            if alerts:
                # cheapest first; everything is marked as notified so nothing trickles in later runs
                alerts.sort(key=lambda a: a.offer.price)
                alerts = self._gate(w, alerts, keep=limit)
                send, rest = alerts[:limit], alerts[limit:]
                msg = format_alerts(w.name, send, self.store_names)
                if rest:
                    msg += f"\n\n…и ещё {len(rest)} подходящих (дороже). Список: /top {w.id}"
                if self._send(msg):
                    for a in alerts:
                        self.state.mark_notified(a)
                    summary["alerts"] += len(send)
                else:
                    for a in alerts:
                        self.state.mark_pending(a)   # retried on the next run

        self._store_health(runs)
        self._url_health(watches, runs)
        for k, r in runs.items():
            summary["stores"][k] = {
                "ok": not r.errors or bool(r.results) or any(r.products.values()),
                "offers": sum(len(v) for v in r.results.values()),
                "errors": r.errors, "sec": round(r.seconds, 1), "requests": r.requests,
            }
        if self._bad_urls:
            summary["stores"]["(bad links)"] = {"ok": False, "offers": 0, "errors": dict(self._bad_urls),
                                                "sec": 0.0, "requests": 0}
        self.state.prune(_setting(self.settings, "history_days", 45, int), {w.id for w in self.all_watches()})
        self.state.data["runs"] = self.state.data.get("runs", 0) + 1
        self.state.data["last_run"] = int(time.time())
        self.state.data["last_summary"] = {
            "watches": summary["watches"], "alerts": summary["alerts"], "matches": summary["matches"],
            "seconds": round(time.monotonic() - t0, 1),
        }
        summary["seconds"] = round(time.monotonic() - t0, 1)
        return summary

    # ------------------------------------------------------------------ health messages
    def _gate(self, w: Watch, alerts: list, keep: int) -> list:
        """Claude's last look at what is about to be sent. Rejected offers are recorded as not matching and
        silently marked notified, so they never reach the user (or the Mini App's best-price list)."""
        from . import ai

        if not alerts or not ai.enabled():
            return alerts
        head = alerts[: max(keep * 2, keep + 6)]      # judge the ones that would actually be shown, plus spares
        try:
            verdict = ai.gate(self.state, w, [a.offer for a in head])
        except Exception as e:
            log.warning("AI gate failed for %s: %s", w.id, e)
            return alerts
        out = []
        for a in alerts:
            if verdict.get(a.offer.key) is False:
                rec = self.state.offers.get(self.state.rec_key(w, a.offer))
                if rec is not None:
                    rec["m"] = False
                    rec["np"] = a.offer.price
                    rec["rej"] = 1
                continue
            out.append(a)
        if len(out) != len(alerts):
            log.info("AI gate %s: %d of %d offers rejected", w.id, len(alerts) - len(out), len(alerts))
        return out

    def _ai_judge(self, w: Watch, candidates: list) -> dict:
        """Claude verdicts for titles that already pass the cheap price/stock rules (cached per watch)."""
        from . import ai

        if not ai.enabled():   # with a key, Claude judges every watch (not only the ones it parsed)
            return {}
        titles = []
        for o, check_title in candidates:
            if not check_title:
                continue
            if w.max_price is not None and o.price > w.max_price * 1.02:
                continue
            if w.min_price is not None and o.price < w.min_price:
                continue
            if w.in_stock and o.in_stock is False:
                continue
            titles.append(o.title)
        try:
            out = ai.verdicts(self.state, w, titles)
        except Exception as e:   # never let the AI step break a run
            log.warning("AI judge failed for %s: %s", w.id, e)
            out = {}
        out["__ai_on__"] = {"match": False}
        return out

    def _store_health(self, runs: dict[str, StoreRun]) -> None:
        threshold = max(1, _setting(self.settings, "store_fail_alert_after", 6, int))
        ts = time.time()
        down: list[tuple[dict, str]] = []
        up: list[tuple[dict, str]] = []
        any_blocked = False
        for k, r in runs.items():
            if k.startswith("web:") and not r.results:
                continue   # ad-hoc shops of URL watches: reported per link by _url_health
            if not r.attempted:
                continue   # everything skipped by the time budget: says nothing about the store
            failed = bool(r.errors) and not r.results and not any(r.products.values())
            if failed and not r.searched and not r.store_failed:
                continue   # only product links were checked and they failed for link reasons (404, no price on
                #            the page): the shop itself may be fine, _url_health reports each link
            err = "; ".join(sorted({v for q, v in r.errors.items()
                                    if q not in r.skipped and not v.startswith("skipped (")}))[:300]
            h = self.state.store_result(k, not failed, err, sum(len(v) for v in r.results.values()))
            name = self.store_names.get(k, k)
            if failed and h["cf"] >= threshold and not _warned(h):
                if ts - float(h.get("alerted_at") or 0) >= WARN_COOLDOWN:
                    any_blocked = any_blocked or r.blocked
                    down.append((h, f"• <b>{esc(name)}</b> — {h['cf']} проверок подряд: <code>{esc(err[:120])}</code>"))
                else:
                    # warned less than 24 h ago and it flapped back: stay quiet (also on recovery) until the
                    # cooldown is over; if it is still down then, the warning goes out
                    h["alerted"] = True
                    h["warned"] = False
            elif not failed and h.get("alerted"):
                if h.get("warned", True):          # old state files: alerted always meant "warning sent"
                    up.append((h, esc(name)))
                else:
                    h["alerted"] = False
        if down:
            hint = ("\nПохоже, сайт блокирует запросы из облака GitHub. Цены этого магазина часто видны через "
                    "агрегаторы (compari.ro); иначе поможет прокси или запуск дома — см. README." if any_blocked else "")
            if self._send("⚠️ Магазины не отвечают:\n" + "\n".join(line for _, line in down) + hint):
                for h, _ in down:
                    h.update(alerted=True, warned=True, alerted_at=int(ts))
            # not delivered: "alerted" stays False, so the warning is tried again next run
        if up:
            if self._send("✅ Снова работают: " + ", ".join(name for _, name in up)):
                for h, _ in up:
                    h.update(alerted=False, warned=False)

    def _url_health(self, watches: list[Watch], runs: dict[str, StoreRun]) -> None:
        """One message when a watched product link cannot be read N runs in a row, one when it works again."""
        threshold = max(1, _setting(self.settings, "store_fail_alert_after", 6, int))
        ts = time.time()
        index = {w.id: i for i, w in enumerate(self.all_watches(), 1)}
        for w in watches:
            for u in w.urls:
                if u in self._bad_urls:
                    ok, err = False, self._bad_urls[u]
                else:
                    r = next((r for r in runs.values() if u in r.products or u in r.errors), None)
                    if r is None or u in r.skipped:
                        continue       # not checked this run (time budget): no news
                    ok = r.products.get(u) is not None
                    if not ok and r.store_failed and not r.key.startswith("web:"):
                        continue       # the whole shop is blocked / down: one "Магазины не отвечают" message
                        #                from _store_health, not one "check your link" message per link
                    err = r.errors.get(u, "")
                    if err.startswith("skipped (") and r.cause:
                        err = r.cause
                h = self.state.url_result(w.id, u, ok, err)
                n = index.get(w.id, "N")
                if not ok and h["cf"] >= threshold and not _warned(h):
                    if ts - float(h.get("alerted_at") or 0) < WARN_COOLDOWN:
                        h.update(alerted=True, warned=False)
                        continue
                    hint = ("\nПохоже, сайт блокирует запросы из облака GitHub: поможет прокси или запуск дома "
                            "(см. README)." if err.startswith("BLOCKED") else "")
                    msg = (f"⚠️ <b>{esc(w.name)}</b>: не удаётся прочитать цену по ссылке {esc(u)} "
                           f"({h['cf']} проверок подряд): <code>{esc(err[:150])}</code>{hint}\n"
                           f"Проверьте ссылку (заменить: <code>/set {n} новая_ссылка</code>) "
                           f"или удалите пункт: /del {n}")
                    if self._send(msg):
                        h.update(alerted=True, warned=True, alerted_at=int(ts))
                elif ok and h.get("alerted"):
                    if not h.get("warned", True) or self._send(
                            f"✅ <b>{esc(w.name)}</b>: цена по ссылке снова читается ({esc(u)})."):
                        h.update(alerted=False, warned=False)

    def _send_notices(self) -> None:
        notices = self.state.data.get("notices")
        if not notices or getattr(self, "home_mode", False):   # home runner: the main state's notices are
            return                                              # the cloud runner's to send
        if self._send("\n\n".join(notices)):
            self.state.data["notices"] = []

    # ------------------------------------------------------------------ delivery
    def _send(self, text: str) -> bool:
        """Deliver a message. True only if it reached Telegram (or in dry-run mode); callers record
        "notified" / "initialized" / "warned" only on True, so a failed message is retried next run."""
        if self.dry_run:
            print("---- [dry-run message] ----\n" + text + "\n---------------------------")
            return True
        chat = self.state.get("telegram", "chat_id")
        try:
            silent = in_quiet_hours(self.settings.quiet_hours)
        except Exception:  # never let the quiet-hours lookup stop a deal alert
            silent = False
        markup = None
        factory = getattr(self, "markup_factory", None)
        if factory:
            try:   # every message carries the up-to-date "⚙️ Управление" button
                markup = factory()
            except Exception:
                markup = None
        if self.tg.chat_ids:
            res = self.tg.send(text, silent=silent, markup=markup)
        elif chat:
            res = self.tg.send(text, chat_id=str(chat), silent=silent, markup=markup)
        else:
            print("---- [no chat bound yet: will be sent after /start] ----\n" + text)
            return False
        return res is not False   # Telegram.send returns a bool; a None from older fakes counts as delivered


def _warned(h: dict) -> bool:
    """A warning was sent for the current failure streak (old state files: alerted alone meant that)."""
    return bool(h.get("alerted")) and bool(h.get("warned", True))


def _watch_to_spec(w: Watch) -> dict:
    d = w.to_dict()
    d.pop("origin", None)
    return d


def _stored_watch(d: dict, origin: str) -> Watch:
    """Re-load a watch that was accepted earlier (Telegram watch or override kept in state) without the input
    checks of /add, so state written by older versions keeps loading: a watch with a link that the new checks
    reject stays visible in /list (and deletable) and its link is reported by _url_health instead of the watch
    silently disappearing."""
    try:
        return watch_from_dict(d, origin=origin, validate=False)
    except TypeError as e:
        if "validate" not in str(e):   # config.watch_from_dict without the `validate` option
            raise
        return watch_from_dict(d, origin=origin)
