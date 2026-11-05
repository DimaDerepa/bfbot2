"""Command line entry point.

  python -m bot run      [--config config.yaml] [--state state.json] [--dry-run]   one cycle (GitHub Actions)
  python -m bot daemon   [--config ...] [--state data/state.json]                  forever (VPS / home server)
  python -m bot search   STORE|all "query" [--limit 10]                           manual test of adapters
  python -m bot product  URL                                                       price of one product page
  python -m bot diag     --query "iphone 16" [--stores a,b] [--out diag]          save raw pages + parse report
  python -m bot stores                                                             list adapters
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from .config import load_config
from .engine import Engine, http_for_store
from .notify import Telegram
from .prices import fmt_price
from .state import State
from .stores import IMPORT_ERRORS, registry, store_for_url, web_store_for
from .tg_commands import CommandProcessor

log = logging.getLogger("bot")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print_summary(summary: dict) -> None:
    print(f"\n=== cycle done in {summary.get('seconds')}s: {summary['watches']} watches, "
          f"{summary['matches']} matching offers, {summary['alerts']} alerts ===")
    for k, s in sorted(summary.get("stores", {}).items()):
        status = "OK " if s["ok"] else "ERR"
        err = ("  " + "; ".join(f"{q}: {e}" for q, e in s["errors"].items()))[:300] if s["errors"] else ""
        print(f"  [{status}] {k:<14} offers={s['offers']:<4} req={s['requests']:<3} {s['sec']:>5}s{err}")


UI_VERSION = 5
UI_NEWS = ("🆕 <b>Новое приложение.</b> Нажмите кнопку <b>🎯 Открыть приложение</b> внизу чата: там все товары с лучшими "
           "ценами, условия, магазины и настройки. Добавить товар можно на вкладке ➕ или просто написав мне, что ищете, "
           "обычными словами. Теперь я отвечаю сразу.")
SPEC_VERSION = 2


def _migrate_watches(state) -> None:
    """Re-derive the criteria of Telegram watches with the stricter rules (product type at the start of the
    title, every Latin word required, screen size), drop exact duplicates and forget their old (loose) matches."""
    from . import nlp_ru
    from .text import has_cyrillic

    seen: set = set()
    keep = []
    changed = []
    for d in state.data.get("watches", []):
        sig = (d.get("query"), d.get("max_price"), d.get("min_price"), tuple(sorted(d.get("exclude") or [])), tuple(d.get("urls") or []))
        if sig in seen:
            state.forget_watch(d.get("id", ""))
            continue
        seen.add(sig)
        text = d.get("intent") or d.get("name") or ""
        if not d.get("ai") and has_cyrillic(text):
            try:
                spec = nlp_ru.parse(text.rstrip("…"))
            except ValueError:
                spec = {}
            for k in ("must_include", "screen_inch", "ram_min", "year_min", "categories", "intent"):
                if spec.get(k):
                    d[k] = spec[k]
            for k in ("exclude", "any_of"):
                if spec.get(k):
                    d[k] = sorted(set(d.get(k) or []) | set(spec[k]))
            changed.append(d.get("id"))
        keep.append(d)
    state.data["watches"] = keep
    for wid in changed:
        state.forget_watch(wid)          # old loose matches out; a fresh, clean digest follows
    state.data["spec_version"] = SPEC_VERSION
    if changed:
        state.data.setdefault("notices", []).append(
            "🔧 Фильтры стали строже: я перепроверил ваши товары и пришлю новые подборки без аксессуаров и лишнего.")


def _ai_reparse(state) -> None:
    """Once a Claude key is available, let Claude re-read every Telegram watch from the user's own words."""
    from . import ai
    from .config import watch_from_dict

    if not ai.enabled():   # usage + budget were loaded by cmd_run
        return
    redone = []
    tried = state.data.setdefault("ai_reparsed", [])
    for d in state.data.get("watches", []):
        if d.get("ai") or d.get("id") in tried:
            continue
        text = (d.get("intent") or d.get("name") or d.get("query") or "").strip()
        tried.append(d.get("id"))
        if not text or d.get("urls"):
            continue
        parsed = ai.parse_requests(text)
        if not parsed:
            continue
        spec = ai.spec_from_ai(parsed[0], text)
        trial = {**{k: v for k, v in d.items() if k not in ("origin",)}, **spec, "id": d.get("id")}
        for k in ("must_include", "screen_inch", "ram_min", "year_min"):   # Claude's spec replaces the rule-based one
            if k not in spec:
                trial.pop(k, None)
        try:
            watch_from_dict(trial, origin="telegram")
        except (ValueError, TypeError):
            continue
        d.clear()
        d.update(trial)
        state.forget_watch(d["id"])
        redone.append(d.get("name") or text)
    if redone:
        from .notify import esc

        state.data.setdefault("notices", []).append(
            "🤖 Claude заново разобрал ваши запросы и будет проверять каждое предложение: "
            + ", ".join(f"<b>{esc(n)}</b>" for n in redone) + ". Скоро пришлю новые подборки.")


HOME_KEYS = ("offers", "initialized", "stores", "runs", "last_run", "last_summary", "url_fail", "events_applied",
             "ai_usage", "ai_cache", "ai_gate", "model_np", "alert_refs", "ai_fail_runs")


HOME_STALE_H = 3


def _check_home_runner(state, home_data: dict) -> None:
    """Hybrid mode with the home PC off / broken: the cloud checks the home shops itself again (they may block it,
    but that beats not checking eMAG at all) and says so once a day."""
    if os.environ.get("BOT_HOME_ENABLED", "").lower() not in ("1", "true", "yes"):
        return
    last = home_data.get("last_run") if isinstance(home_data, dict) else None
    age_h = (time.time() - float(last)) / 3600 if last else None
    if age_h is not None and age_h < HOME_STALE_H:
        state.data.pop("home_stale_warned", None)
        return
    os.environ["BOT_HOME_ENABLED"] = "stale"     # Engine / Mini App: home shops are checked from the cloud now
    if state.get("telegram", "chat_id") and time.time() - float(state.data.get("home_stale_warned", 0) or 0) > 86400:
        when = f"уже {age_h:.0f} ч" if age_h is not None else "ещё ни разу"
        state.data.setdefault("notices", []).append(
            f"🏠 Домашний компьютер не проверял цены {when}. Пока проверяю eMAG, PC Garage и др. из облака "
            "(они могут блокировать). Включите компьютер или проверьте раннер (Settings → Actions → Runners).")
        state.data["home_stale_warned"] = int(time.time())


def _home_state_path(main_path: str) -> str:
    return os.environ.get("BOT_HOME_STATE") or os.path.join(os.path.dirname(os.path.abspath(main_path)), "state-home.json")


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    state = State.load(args.state)
    home_mode = os.environ.get("BOT_MODE", "").lower() == "home"
    home_path = _home_state_path(args.state)
    peer_usage = None
    if home_mode:
        # watches/chat come from the main state (read-only here); offers & health live in state-home.json
        home = State.load(home_path)
        # the cloud runner's offers / announced models: one alert per model across both runners
        state.data["peer_offers"] = state.data.get("offers", {})
        state.data["peer_model_np"] = state.data.get("model_np", {})
        peer_usage = state.data.get("ai_usage")
        for k in HOME_KEYS:
            if k in home.data:
                state.data[k] = home.data[k]
            else:
                state.data.pop(k, None)
        state._defaults()
        # watches deleted + re-added (or renamed) through Telegram since the last home run: reset / move their
        # home offers too, so the home shops send the new watch's digest (the cloud runner already did)
        state.data["events_applied"] = state.replay_watch_events(
            state.data.get("watch_events"), state.data.get("watch_seq"), state.data.get("events_applied"))
        state.path = home_path
    else:
        hd: dict = {}
        if os.path.exists(home_path):
            try:
                hd = State.load(home_path).data
                state.data["peer_offers"] = hd.get("offers", {})
                state.data["peer_model_np"] = hd.get("model_np", {})
                state.data["peer_alert_refs"] = hd.get("alert_refs", {})   # 👎 under the home runner's alerts
                peer_usage = hd.get("ai_usage")
            except Exception:
                hd = {}
        _check_home_runner(state, hd)
    # --dry-run must not touch the real bot: no getUpdates (it would confirm and swallow the production
    # bot's pending commands), no replies, no alerts
    tg = Telegram(None) if args.dry_run else Telegram.from_env()
    engine = Engine(cfg, state, tg, dry_run=args.dry_run)
    from . import ai as _ai

    # one daily budget for both runners: the other one's spend today counts too
    _ai.load_usage(state, getattr(engine.settings, "ai_daily_budget_usd", 1.0), _ai.spent_today(peer_usage))
    if IMPORT_ERRORS:
        print("store import errors:", IMPORT_ERRORS, file=sys.stderr)
    cp = CommandProcessor(engine, mode="actions")
    listen = 0.0 if (home_mode or args.dry_run) else float(os.environ.get("BOT_LISTEN_MINUTES") or 0) * 60
    t_end = time.time() + listen
    if not home_mode and state.data.get("spec_version", 0) < SPEC_VERSION:
        _migrate_watches(state)
    if not home_mode and not args.dry_run:
        _ai_reparse(state)
    if not home_mode and state.get("telegram", "chat_id") and state.data.get("ui_version") != UI_VERSION:
        # one-time hello after an update: delivers the new menu (with the Mini App button) without any /command
        state.data.setdefault("notices", []).append(UI_NEWS)
        state.data["ui_version"] = UI_VERSION
    try:
        if not home_mode:
            n = cp.poll(timeout=0)
            if n:
                log.info("processed %d telegram messages", n)
            state.save()
        summary = engine.run_cycle()
        _print_summary(summary)
        state.save()
        # keep answering buttons/commands until the next scheduled run takes over
        while not home_mode and time.time() < t_end - 5:
            cp.force_check = False
            n = cp.poll(timeout=int(max(1, min(25, t_end - time.time() - 3))))
            if n:
                state.save()
                if cp.force_check and t_end - time.time() > 150:
                    # a product was added/changed: check it right away instead of waiting for the next run
                    summary = engine.run_cycle()
                    _print_summary(summary)
                    state.save()
            elif n is None:
                time.sleep(5)
        if not home_mode:
            cp.poll(timeout=0)
    finally:
        _ai.save_usage(state)
        if not home_mode:
            try:
                state.data["app"] = cp.ui.webapp_snapshot()   # the Mini App reads this from the state branch
            except Exception:
                pass
        state.save()
    return 0


def poll_backoff(fails: int) -> float:
    """Pause after `fails` failed getUpdates calls in a row: 5 s, 10 s, 20 s ... at most 5 min."""
    return float(min(300, 5 * 2 ** max(0, fails - 1))) if fails > 0 else 0.0


def cycle_backoff(fails: int, interval: float) -> float:
    """Delay before re-running a cycle that crashed `fails` times in a row: 1, 2, 4 ... min, at most the interval."""
    return float(max(60, min(interval, 60 * 2 ** max(0, fails - 1))))


def cmd_daemon(args, *, sleep=None, max_loops: int | None = None) -> int:
    sleep = sleep or time.sleep
    cfg = load_config(args.config)
    state = State.load(args.state)
    tg = Telegram.from_env()
    try:
        interval = int(os.environ.get("BOT_INTERVAL_MINUTES") or cfg.settings.interval_minutes) * 60
    except (TypeError, ValueError):
        interval = 600
    interval = max(60, interval)
    next_run = 0.0
    tg_fails = 0       # failed getUpdates in a row (bad token, Telegram 5xx, 409 from a second instance)
    cycle_fails = 0    # crashed cycles in a row
    loop_fails = 0     # crashes outside the cycle in a row (Engine / CommandProcessor could not be built)
    loops = 0
    log.info("daemon started, interval %ds", interval)
    try:
        while max_loops is None or loops < max_loops:
            loops += 1
            try:
                cfg = load_config(args.config)  # pick up edits without restart
            except Exception as e:
                log.error("config error (keeping previous): %s", e)
            pause = 0.0
            try:
                engine = Engine(cfg, state, tg)
                from . import ai as _ai

                _ai.load_usage(state, getattr(engine.settings, "ai_daily_budget_usd", 1.0))
                cp = CommandProcessor(engine, mode="daemon")
                if tg.enabled:
                    try:
                        n = cp.poll(timeout=20)
                    except Exception:
                        traceback.print_exc()
                        n = None
                    if n is None:
                        tg_fails += 1
                        pause = poll_backoff(tg_fails)
                        log.warning("telegram getUpdates failed %d time(s) in a row; next try in %.0fs",
                                    tg_fails, pause)
                    else:
                        tg_fails = 0
                    state.save_if_changed()
                if cp.force_check or time.time() >= next_run:
                    try:
                        summary = engine.run_cycle()
                        _print_summary(summary)
                        cycle_fails = 0
                        next_run = time.time() + interval
                    except Exception:
                        traceback.print_exc()
                        cycle_fails += 1
                        # never hammer the shops faster than the interval because a cycle keeps crashing
                        next_run = time.time() + cycle_backoff(cycle_fails, interval)
                        log.error("cycle crashed %d time(s) in a row; next try in %.0fs", cycle_fails,
                                  next_run - time.time())
                    _ai.save_usage(state)
                    state.save_if_changed()
            except Exception:
                # e.g. the Engine cannot even be built (broken shop entry): no cycle ran, next_run did not move,
                # so without this back-off the loop would spin every second
                traceback.print_exc()
                loop_fails += 1
                state.save_if_changed()
                pause = max(pause, min(300.0, 30.0 * 2 ** (loop_fails - 1)))
            else:
                loop_fails = 0
            if not tg.enabled:
                pause = max(pause, min(30.0, next_run - time.time()), 1.0)
            if pause > 0:
                left = next_run - time.time()
                # do not oversleep the next cycle (unless the loop itself crashed: then wait the full pause)
                sleep(pause if loop_fails else (max(1.0, min(pause, left)) if left > 0 else 1.0))
    except KeyboardInterrupt:
        pass
    state.save()
    return 0


def _store_instance(cls, cfg):
    return cls(http_for_store(cls, cfg.settings), max_pages=cfg.settings.max_pages)


def cmd_search(args) -> int:
    cfg = load_config(args.config)
    reg = registry(cfg.settings.shops)
    keys = list(reg) if args.store == "all" else [s.strip() for s in args.store.split(",")]
    rc = 0
    for k in keys:
        if k not in reg:
            print(f"unknown store {k}; known: {', '.join(sorted(reg))}")
            rc = 2
            continue
        st = _store_instance(reg[k], cfg)
        t0 = time.monotonic()
        try:
            offers = st.search(args.query)
        except Exception as e:
            print(f"[{k}] ERROR {type(e).__name__}: {e}")
            rc = 1
            continue
        print(f"[{k}] {len(offers)} offers in {time.monotonic() - t0:.1f}s via {st.last_strategy or '?'}")
        for o in offers[: args.limit]:
            old = f" (was {fmt_price(o.old_price)})" if o.old_price else ""
            stock = {True: "in stock", False: "OUT", None: "?"}[o.in_stock]
            print(f"   {fmt_price(o.price):>10} lei{old}  [{stock}]  {o.title[:90]}\n              {o.url}")
    return rc


def cmd_product(args) -> int:
    cfg = load_config(args.config)
    reg = registry(cfg.settings.shops)
    cls = store_for_url(args.url, reg) or web_store_for(args.url)
    st = _store_instance(cls, cfg)
    o = st.product(args.url)
    if not o:
        print("price not found")
        return 1
    print(json.dumps(o.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_diag(args) -> int:
    """Fetch every store once, save raw responses and a parse report (used to fix adapters)."""
    cfg = load_config(args.config)
    reg = registry(cfg.settings.shops)
    keys = list(reg) if not args.stores or args.stores == "all" else [s.strip() for s in args.stores.split(",")]
    os.makedirs(args.out, exist_ok=True)
    report: dict = {"query": args.query, "time": int(time.time()), "import_errors": IMPORT_ERRORS, "stores": {}}

    def one(k: str) -> tuple[str, dict]:
        cls = reg[k]
        st = _store_instance(cls, cfg)
        st.max_pages = 1
        info: dict = {"name": cls.name, "base_url": cls.base_url}
        t0 = time.monotonic()
        try:
            offers = st.search(args.query)
            info.update(ok=True, count=len(offers), strategy=st.last_strategy,
                        sample=[o.to_dict() for o in offers[:8]])
        except Exception as e:
            info.update(ok=False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-1500:])
        info["seconds"] = round(time.monotonic() - t0, 1)
        info["browser_used"] = getattr(st.http, "browser_used", 0)
        lr = st.http.last_response
        if lr is not None:
            info.update(status=lr.status, final_url=lr.url, bytes=len(lr.text),
                        content_type=lr.headers.get("content-type") or lr.headers.get("Content-Type"))
            ext = "json" if lr.text.lstrip()[:1] in "{[" else "html"
            with open(os.path.join(args.out, f"{k}.{ext}"), "w", encoding="utf-8") as f:
                f.write(lr.text[:4_000_000])
        return k, info

    with ThreadPoolExecutor(max_workers=6) as ex:
        for k, info in ex.map(one, [k for k in keys if k in reg]):
            report["stores"][k] = info
            flag = "OK " if info.get("ok") and info.get("count") else ("EMPTY" if info.get("ok") else "ERR")
            print(f"[{flag}] {k:<14} status={info.get('status')} count={info.get('count')} "
                  f"strategy={info.get('strategy')} {info.get('error', '')[:150]}")
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    lines = [f"# Diagnostics: `{args.query}`\n", "| store | ok | HTTP | offers | strategy | browser | sec | error |",
             "|---|---|---|---|---|---|---|---|"]
    for k, i in sorted(report["stores"].items()):
        lines.append(f"| {k} | {'✅' if i.get('ok') and i.get('count') else ('⚠️' if i.get('ok') else '❌')} | {i.get('status')} | "
                     f"{i.get('count', '')} | {i.get('strategy', '')} | {i.get('browser_used') or ''} | {i.get('seconds')} | "
                     f"{(i.get('error') or '')[:80]} |")
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0


def cmd_stores(args) -> int:
    cfg = load_config(args.config)
    reg = registry(cfg.settings.shops)
    if getattr(args, "markdown", False):
        print("| ключ | магазин | категории | тип |\n|---|---|---|---|")
        for k, cls in sorted(reg.items(), key=lambda kv: (not kv[1].aggregator, kv[0])):
            kind = "агрегатор цен" if cls.aggregator else ("выключен по умолчанию" if not cls.enabled_by_default else "магазин")
            print(f"| `{k}` | [{cls.name}]({cls.base_url}) | {', '.join(cls.categories)} | {kind} |")
        return 0
    for k, cls in sorted(reg.items()):
        print(f"{k:<14} {cls.name:<22} {'aggregator ' if cls.aggregator else ''}{','.join(cls.categories)}  {cls.base_url}")
    if IMPORT_ERRORS:
        print("import errors:", IMPORT_ERRORS)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bot", description="Black Friday price tracker for Romanian shops")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--config", default=os.environ.get("BOT_CONFIG", "config.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--state", default=os.environ.get("BOT_STATE", "state.json"))
    r.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("daemon")
    d.add_argument("--state", default=os.environ.get("BOT_STATE", "data/state.json"))
    d.set_defaults(fn=cmd_daemon)

    s = sub.add_parser("search")
    s.add_argument("store")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_search)

    pr = sub.add_parser("product")
    pr.add_argument("url")
    pr.set_defaults(fn=cmd_product)

    g = sub.add_parser("diag")
    g.add_argument("--query", default="iphone 16")
    g.add_argument("--stores", default="all")
    g.add_argument("--out", default="diag")
    g.set_defaults(fn=cmd_diag)

    st = sub.add_parser("stores")
    st.add_argument("--markdown", action="store_true", help="print a Markdown table (for README)")
    st.set_defaults(fn=cmd_stores)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.fn(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
