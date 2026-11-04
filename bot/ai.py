"""Claude API helpers: understand a free-text shopping request, and judge product titles against it.

Enabled when ANTHROPIC_API_KEY is set (GitHub secret). Model: BOT_AI_MODEL (default claude-opus-5-5;
claude-haiku-5-5 is a much cheaper option for the bulk title checks: BOT_AI_CLASSIFY_MODEL).
Every title is judged once per watch and cached in the state, so repeated runs cost almost nothing.
Without a key the bot falls back to the rule-based parser (nlp_ru) and the model table (specs).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("bot.ai")

PARSE_MODEL = os.environ.get("BOT_AI_MODEL") or "claude-opus-5-5"
CLASSIFY_MODEL = os.environ.get("BOT_AI_CLASSIFY_MODEL") or PARSE_MODEL
BATCH = 40
MAX_NEW_PER_WATCH = 200   # cap per run so a huge listing cannot run up the bill

CATEGORIES = ["electronics", "pc", "appliances", "home", "fashion", "beauty", "diy", "toys", "sport", "books", "general"]


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client():
    import anthropic

    return anthropic.Anthropic(max_retries=2, timeout=90)


def _json_call(model: str, system: str, user: str, schema: dict, max_tokens: int, effort: str) -> Optional[dict]:
    import anthropic

    client = _client()
    kw: dict[str, Any] = dict(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    try:
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
    except TypeError:  # older SDK without the fallbacks parameter
        resp = client.messages.create(**kw)
    except anthropic.APIStatusError as e:
        log.warning("claude API error %s: %s", getattr(e, "status_code", "?"), e)
        return None
    except anthropic.APIConnectionError as e:
        log.warning("claude API connection error: %s", e)
        return None
    if resp.stop_reason == "refusal":
        log.warning("claude refused: %s", getattr(resp, "stop_details", None))
        return None
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        log.warning("claude returned non-JSON: %r", text[:200])
        return None


# ------------------------------------------------------------------ request -> spec
_N = {"type": ["number", "null"]}
_I = {"type": ["integer", "null"]}
_S = {"type": "array", "items": {"type": "string"}}
PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "search_queries": {"type": "array", "items": {"type": "string"},
                           "description": "1-3 short Romanian search phrases exactly as typed into a Romanian shop's search box"},
        "product_type": {"type": "string", "description": "what kind of product, in Romanian, e.g. 'telefon mobil'"},
        "category": {"type": "string", "enum": CATEGORIES},
        "max_price": _N, "min_price": _N,
        "currency": {"type": "string", "enum": ["RON", "USD", "EUR", "GBP"]},
        "min_discount_pct": _N,
        "ram_min_gb": _I, "storage_min_gb": _I, "year_min": _I,
        "brands_include": _S, "brands_exclude": _S, "exclude_words": _S,
        "other_requirements": {"type": "string", "description": "any other hard requirement, in English, or ''"},
        "summary_ru": {"type": "string", "description": "short Russian name for this watch, max 50 chars"},
    },
    "required": ["search_queries", "product_type", "category", "max_price", "min_price", "currency", "min_discount_pct",
                 "ram_min_gb", "storage_min_gb", "year_min", "brands_include", "brands_exclude", "exclude_words",
                 "other_requirements", "summary_ru"],
    "additionalProperties": False,
}
PARSE_SYSTEM = (
    "You turn a shopper's request (usually Russian, sometimes Romanian/English) into a structured watch for a bot that "
    "searches Romanian online shops (eMAG, Altex, Flanco, PC Garage, ...) for Black Friday deals. Shop titles are in "
    "Romanian, e.g. 'Telefon mobil Samsung Galaxy S25, 12GB RAM, 256GB, 5G'. search_queries must be what a person types "
    "into those shops' search boxes (Romanian words, brand/model names, specs like '12gb ram'); keep them short and broad "
    "enough to list many matching products. Prices: keep the number and the currency the user used. 'не X' / 'без X' / "
    "'кроме X' are exclusions. Use null when the user did not ask for something. Brand names in lowercase Latin."
)


def parse_request(text: str) -> Optional[dict]:
    if not enabled():
        return None
    data = _json_call(PARSE_MODEL, PARSE_SYSTEM, text, PARSE_SCHEMA, 2000, "medium")
    if not data or not data.get("search_queries"):
        return None
    return data


def spec_from_ai(data: dict, original: str) -> dict:
    """AI parse -> watch dict for watch_from_dict()."""
    from .fx import to_ron

    cur = data.get("currency") or "RON"
    q = [s.strip() for s in data.get("search_queries") or [] if s and s.strip()]
    spec: dict[str, Any] = {
        "name": (data.get("summary_ru") or original)[:60],
        "query": q[0],
        "extra_queries": q[1:3],
        "categories": [data["category"]] if data.get("category") and data["category"] != "general" else None,
        "exclude": list(dict.fromkeys([*(data.get("brands_exclude") or []), *(data.get("exclude_words") or [])])),
        "any_of": data.get("brands_include") or [],
        "ram_min": data.get("ram_min_gb"),
        "year_min": data.get("year_min"),
        "intent": original,
        "ai": True,
        "strict": False,
        "pages": 3,
    }
    for src, dst in (("max_price", "max_price"), ("min_price", "min_price")):
        if data.get(src):
            spec[dst] = to_ron(float(data[src]), cur)
    if data.get("min_discount_pct"):
        spec["min_discount"] = float(data["min_discount_pct"])
    if data.get("storage_min_gb"):
        spec["storage_min"] = int(data["storage_min_gb"])
    if data.get("other_requirements"):
        spec["notes"] = data["other_requirements"]
    return {k: v for k, v in spec.items() if v not in (None, [], "")}


# ------------------------------------------------------------------ title classification
CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "match": {"type": "boolean"}, "year": _I, "ram_gb": _I,
                       "why": {"type": "string"}},
        "required": ["i", "match", "year", "ram_gb", "why"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}
CLASSIFY_SYSTEM = (
    "You check product listings from Romanian online shops against a shopper's request. For every numbered title decide "
    "match=true only if the listing IS the requested kind of product (not an accessory, case, spare part, service or bundle "
    "of a different product) AND it satisfies every hard requirement you can verify from the title plus your knowledge of "
    "the model (release year = first global launch year of that model/variant; RAM of that variant). Price and discount are "
    "checked elsewhere, ignore them. year/ram_gb: your best knowledge, null if you really do not know. If a requirement "
    "cannot be determined at all, match=false. why: max 8 words."
)


def _key(title: str) -> str:
    return hashlib.sha1(" ".join(title.lower().split()).encode()).hexdigest()[:16]


def _requirements(w) -> str:
    parts = [f"Request: {w.intent or w.query}"]
    if getattr(w, "ram_min", None):
        parts.append(f"RAM >= {w.ram_min} GB")
    if getattr(w, "storage_min", None):
        parts.append(f"storage >= {w.storage_min} GB")
    if getattr(w, "year_min", None):
        parts.append(f"model released in {w.year_min} or later")
    if w.any_of:
        parts.append("brand must be one of: " + ", ".join(w.any_of))
    if w.exclude:
        parts.append("must NOT be / contain: " + ", ".join(w.exclude))
    if getattr(w, "notes", None):
        parts.append("Other: " + w.notes)
    return "\n".join(parts)


def verdicts(state, w, titles: list[str]) -> dict[str, dict]:
    """title -> {"match", "year", "ram_gb"} for the given titles (cached per watch)."""
    cache = state.data.setdefault("ai_cache", {}).setdefault(w.id, {})
    sig = _key(_requirements(w))
    if cache.get("_sig") != sig:          # criteria changed -> judge again
        cache.clear()
        cache["_sig"] = sig
    out: dict[str, dict] = {}
    todo: list[str] = []
    for t in dict.fromkeys(titles):
        v = cache.get(_key(t))
        if v is not None:
            out[t] = {"match": v[0], "year": v[1], "ram_gb": v[2]}
        else:
            todo.append(t)
    if todo and enabled():
        todo = todo[:MAX_NEW_PER_WATCH]
        req = _requirements(w)
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            listing = "\n".join(f"{n}. {t}" for n, t in enumerate(chunk))
            data = _json_call(CLASSIFY_MODEL, CLASSIFY_SYSTEM, f"{req}\n\nTitles:\n{listing}", CLASSIFY_SCHEMA,
                              4000, "low")
            if not data:
                break  # API trouble: the rule-based checks are used for the rest this run
            for it in data.get("items", []):
                idx = it.get("i")
                if isinstance(idx, int) and 0 <= idx < len(chunk):
                    t = chunk[idx]
                    cache[_key(t)] = [bool(it.get("match")), it.get("year"), it.get("ram_gb"), int(time.time())]
                    out[t] = {"match": bool(it.get("match")), "year": it.get("year"), "ram_gb": it.get("ram_gb")}
    return out
