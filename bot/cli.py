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
from .engine import Engine
from .net import Http, proxy_for
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


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    state = State.load(args.state)
    tg = Telegram.from_env()
    engine = Engine(cfg, state, tg, dry_run=args.dry_run)
    if IMPORT_ERRORS:
        print("store import errors:", IMPORT_ERRORS, file=sys.stderr)
    cp = CommandProcessor(engine, mode="actions")
    try:
        n = cp.poll(timeout=0)
        if n:
            log.info("processed %d telegram messages", n)
        state.save()
        summary = engine.run_cycle()
        _print_summary(summary)
        # commands that arrived while we were checking
        cp.poll(timeout=0)
    finally:
        state.save()
    return 0


def cmd_daemon(args) -> int:
    cfg = load_config(args.config)
    state = State.load(args.state)
    tg = Telegram.from_env()
    interval = int(os.environ.get("BOT_INTERVAL_MINUTES") or cfg.settings.interval_minutes) * 60
    next_run = 0.0
    log.info("daemon started, interval %ds", interval)
    while True:
        try:
            cfg = load_config(args.config)  # pick up edits without restart
        except Exception as e:
            log.error("config error (keeping previous): %s", e)
        engine = Engine(cfg, state, tg)
        cp = CommandProcessor(engine, mode="daemon")
        try:
            cp.poll(timeout=20 if tg.enabled else 0)
            state.save()
            if cp.force_check or time.time() >= next_run:
                summary = engine.run_cycle()
                _print_summary(summary)
                state.save()
                next_run = time.time() + interval
        except KeyboardInterrupt:
            state.save()
            return 0
        except Exception:
            traceback.print_exc()
            state.save()
            time.sleep(30)
        if not tg.enabled:
            time.sleep(max(1.0, min(30.0, next_run - time.time())))


def _store_instance(cls, cfg):
    http = Http(timeout=cfg.settings.timeout, delay=max(cfg.settings.request_delay, cls.min_delay), proxy=proxy_for(cls.key),
                extra_headers=cls.extra_headers or None, browser_key=cls.key)
    return cls(http, max_pages=cfg.settings.max_pages)


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
    for k, cls in sorted(registry(cfg.settings.shops).items()):
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
    st.set_defaults(fn=cmd_stores)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.fn(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
