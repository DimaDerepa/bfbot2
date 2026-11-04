"""config.yaml loading + parsing of the compact watch syntax used by Telegram /add."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from typing import Any, Optional

import yaml

from .models import Watch

_WATCH_FIELDS = {f.name for f in fields(Watch)}


@dataclass
class Settings:
    default_stores: Any = "all"            # "all" or list of store keys
    default_categories: Optional[list[str]] = None
    disabled_stores: list[str] = field(default_factory=list)
    notify_drop_pct: float = 2.0           # re-alert when price drops >= N% below last alerted price
    first_run_digest: int = 8              # new watch: send the N cheapest matches in one message
    max_alerts_per_watch: int = 12         # per run
    max_pages: int = 1
    request_delay: float = 1.5             # seconds between requests to the same host
    timeout: float = 25.0
    workers: int = 8
    store_fail_alert_after: int = 6        # consecutive failed runs before a warning message
    history_days: int = 45
    interval_minutes: int = 10             # daemon mode only
    store_every: dict = field(default_factory=dict)   # {store_key: N} -> query that store only every N-th run
    # shops that block GitHub's cloud: checked by the home runner (watch-home.yml) when HOME_RUNNER=true
    home_stores: list[str] = field(default_factory=lambda: [
        "emag", "pcgarage", "evomag", "cel", "forit", "vexio", "leroymerlin", "compari", "pricero"])
    quiet_hours: Optional[str] = "23-8"    # Bucharest time: alerts still arrive, but without sound; null = always loud
    shops: list[dict] = field(default_factory=list)   # custom shops (platform adapters)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class Config:
    settings: Settings
    watches: list[Watch]


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def watch_from_dict(d: dict, origin: str = "config") -> Watch:
    d = dict(d)
    aliases = {"include": "must_include", "must": "must_include", "not": "exclude", "max": "max_price",
               "min": "min_price", "discount": "min_discount", "url": "urls", "skip": "skip_stores"}
    for a, real in aliases.items():
        if a in d and real not in d:
            d[real] = d.pop(a)
    for k in ("must_include", "any_of", "exclude", "urls", "skip_stores"):
        if k in d:
            d[k] = _as_list(d[k])
    for k in ("stores", "categories"):
        if k in d and d[k] is not None:
            d[k] = _as_list(d[k]) or None
    for k in ("min_price", "max_price", "min_discount", "drop_pct"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    if "name" not in d:
        d["name"] = d.get("query") or (d.get("urls") or ["watch"])[0]
    unknown = set(d) - _WATCH_FIELDS
    if unknown:
        raise ValueError(f"watch '{d.get('name')}': неизвестные поля {sorted(unknown)}")
    d["origin"] = origin
    return Watch(**d)


def load_config(path: str) -> Config:
    if not os.path.exists(path):
        return Config(Settings(), [])
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    settings = Settings.from_dict(raw.get("settings") or {})
    watches = [watch_from_dict(w) for w in (raw.get("watches") or [])]
    ids = [w.id for w in watches]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"повторяющиеся имена watch: {sorted(dup)}")
    return Config(settings, watches)


# --------------------------------------------------------------------------- compact syntax
# /add iphone 16 pro 256gb <4800 >3000 -max -resigilat +256gb %15 @emag,altex #electronics !all
#   <N / до N / max=N        max price          >N / от N / min=N   min price
#   %N / скидка N            min discount %     -word / -"two words"  exclude
#   +word / +"two words"     must include       @a,b  only these stores   ^a,b skip stores
#   #cat                     categories         !all  include out-of-stock   ~  loose title match
#   ?a|b|c                   any of             http(s)://...  product URL to watch

_TOKEN_RE = re.compile(r'([+\-]?"[^"]+"|\S+)')


def parse_watch_spec(text: str) -> dict:
    spec: dict[str, Any] = {}
    query_words: list[str] = []
    toks = _TOKEN_RE.findall(text.strip())
    i = 0
    while i < len(toks):
        t = toks[i]
        low = t.lower()
        nxt = toks[i + 1] if i + 1 < len(toks) else None

        def num(s: str) -> Optional[float]:
            s = s.replace(" ", "").replace("лей", "").replace("lei", "").replace("ron", "")
            s = s.replace(".", "").replace(",", ".") if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", s) else s.replace(",", ".")
            try:
                return float(s)
            except ValueError:
                return None

        if low.startswith(("http://", "https://")):
            spec.setdefault("urls", []).append(t)
        elif "=" in t and t.split("=", 1)[0].lower() in {
            "max", "min", "discount", "stores", "skip", "exclude", "include", "any_of", "categories", "drop_pct", "name"
        }:
            k, v = t.split("=", 1)
            k = k.lower()
            if k in ("max", "min", "discount", "drop_pct"):
                spec[{"max": "max_price", "min": "min_price", "discount": "min_discount"}.get(k, k)] = num(v)
            elif k in ("stores", "skip", "exclude", "include", "any_of", "categories"):
                key = {"skip": "skip_stores", "include": "must_include"}.get(k, k)
                spec.setdefault(key, []).extend(_as_list(v))
            else:
                spec["name"] = v
        elif low in ("до", "max", "<=") and nxt and num(nxt) is not None:
            spec["max_price"] = num(nxt)
            i += 1
        elif low in ("от", "min", ">=") and nxt and num(nxt) is not None:
            spec["min_price"] = num(nxt)
            i += 1
        elif low in ("скидка", "discount") and nxt and num(nxt.rstrip("%")) is not None:
            spec["min_discount"] = num(nxt.rstrip("%"))
            i += 1
        elif t.startswith("<") and num(t.lstrip("<=")) is not None:
            spec["max_price"] = num(t.lstrip("<="))
        elif t.startswith(">") and num(t.lstrip(">=")) is not None:
            spec["min_price"] = num(t.lstrip(">="))
        elif (t.startswith("%") and num(t[1:]) is not None) or (t.endswith("%") and num(t[:-1]) is not None):
            spec["min_discount"] = num(t.strip("%"))
        elif t.startswith("-") and len(t) > 1 and not t[1:].replace(".", "").isdigit():
            spec.setdefault("exclude", []).append(t[1:].strip('"'))
        elif t.startswith("+") and len(t) > 1:
            spec.setdefault("must_include", []).append(t[1:].strip('"'))
        elif t.startswith("@") and len(t) > 1:
            spec.setdefault("stores", []).extend(_as_list(t[1:].lower()))
        elif t.startswith("^") and len(t) > 1:
            spec.setdefault("skip_stores", []).extend(_as_list(t[1:].lower()))
        elif t.startswith("#") and len(t) > 1:
            spec.setdefault("categories", []).extend(_as_list(t[1:].lower()))
        elif t.startswith("?") and len(t) > 1:
            spec.setdefault("any_of", []).extend([x for x in t[1:].split("|") if x])
        elif low == "!all":
            spec["in_stock"] = False
        elif low == "~":
            spec["strict"] = False
        else:
            query_words.append(t.strip('"'))
        i += 1
    if query_words:
        spec["query"] = " ".join(query_words)
    return spec
