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
PARSE_MULTI_SCHEMA = {
    "type": "object",
    "properties": {"watches": {"type": "array", "items": PARSE_SCHEMA,
                               "description": "one entry per distinct product the user wants tracked (usually 1)"}},
    "required": ["watches"], "additionalProperties": False,
}
PARSE_SYSTEM = (
    "You turn a shopper's request (usually Russian, sometimes Romanian/English) into a structured watch for a bot that "
    "searches Romanian online shops (eMAG, Altex, Flanco, PC Garage, ...) for Black Friday deals. Shop titles are in "
    "Romanian, e.g. 'Telefon mobil Samsung Galaxy S25, 12GB RAM, 256GB, 5G'. search_queries must be what a person types "
    "into those shops' search boxes (Romanian words, brand/model names, specs like '12gb ram'); keep them short and broad "
    "enough to list many matching products. Prices: keep the number and the currency the user used. 'не X' / 'без X' / "
    "'кроме X' are exclusions. Use null when the user did not ask for something. Brand names in lowercase Latin. "
    "Read long requests carefully and keep every condition (sizes, generations, chips, colours, conditions like 'new "
    "only', budget in any currency). If the message asks for several different products, return one watch per product; "
    "conditions said once for all of them apply to each. Put conditions that do not fit a field into "
    "other_requirements, in precise English, so a later checker can verify listings against them."
)


def parse_requests(text: str) -> list[dict]:
    """Free text -> one parsed spec per product the user wants tracked ([] if Claude is unavailable)."""
    if not enabled():
        return []
    data = _json_call(PARSE_MODEL, PARSE_SYSTEM, text, PARSE_MULTI_SCHEMA, 12000, "medium")
    out = [w for w in (data or {}).get("watches", []) if w.get("search_queries")][:5]
    log.info("claude parsed request into %d watch(es)", len(out))
    return out


def parse_request(text: str) -> Optional[dict]:
    got = parse_requests(text)
    return got[0] if got else None


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
                       "model": {"type": "string", "description": "canonical model id, lowercase, without colour, "
                                 "seller, part number or marketing words; same product in another colour/shop -> same string"},
                       "why": {"type": "string"}},
        "required": ["i", "match", "year", "ram_gb", "model", "why"], "additionalProperties": False}}},
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
    parts = [f"Request: {w.intent or w.name or w.query}", f"Shop search query: {w.query}"]
    if getattr(w, "screen_inch", None):
        parts.append(f"screen diagonal about {w.screen_inch} inch")
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
            out[t] = {"match": v[0], "year": v[1], "ram_gb": v[2], "model": v[4] if len(v) > 4 else None}
        else:
            todo.append(t)
    if todo and enabled():
        todo = todo[:MAX_NEW_PER_WATCH]
        log.info("claude judging %d new titles for %s", len(todo), w.id)
        req = _requirements(w)
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            listing = "\n".join(f"{n}. {t}" for n, t in enumerate(chunk))
            data = _json_call(CLASSIFY_MODEL, CLASSIFY_SYSTEM, f"{req}\n\nTitles:\n{listing}", CLASSIFY_SCHEMA,
                              8000, "low")
            if not data:
                break  # API trouble: the rule-based checks are used for the rest this run
            for it in data.get("items", []):
                idx = it.get("i")
                if isinstance(idx, int) and 0 <= idx < len(chunk):
                    t = chunk[idx]
                    model = (it.get("model") or "").strip().lower()[:80] or None
                    cache[_key(t)] = [bool(it.get("match")), it.get("year"), it.get("ram_gb"), int(time.time()), model]
                    out[t] = {"match": bool(it.get("match")), "year": it.get("year"), "ram_gb": it.get("ram_gb"), "model": model}
    return out


# ------------------------------------------------------------------ output gate (last check before alerting)
GATE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "ok": {"type": "boolean"}, "why": {"type": "string"}},
        "required": ["i", "ok", "why"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}
GATE_SYSTEM = (
    "You are the final quality gate of a deal-alert bot for Romanian online shops. The user will receive a Telegram "
    "alert for every listing you approve, so be strict: ok=true only if the listing is EXACTLY the kind of product the "
    "user asked for and meets every stated requirement (type, model, size/diagonal, specs, year, brand rules, "
    "exclusions, price limits). Reject: accessories, spare parts, remotes, mounts, cables, cases, cleaning kits, "
    "services, warranties, bundles built around another product, wrong size/variant, refurbished/resigilat unless "
    "asked, and prices that are implausible for that product (far too low -> accessory/part/error/scam, or a fake "
    "'old price'). When in doubt, reject. why: max 8 words."
)


def gate(state, w, offers: list) -> dict:
    """offer.key -> True/False for the offers about to be sent (cached per offer and price)."""
    if not enabled() or not offers:
        return {}
    cache = state.data.setdefault("ai_gate", {}).setdefault(w.id, {})
    sig = _key(_requirements(w) + f"|{w.min_price}|{w.max_price}|{w.min_discount}")
    if cache.get("_sig") != sig:
        cache.clear()
        cache["_sig"] = sig
    out: dict = {}
    todo = []
    for o in offers:
        ck = _key(f"{o.key}|{int(o.price)}")
        if ck in cache:
            out[o.key] = cache[ck]
        else:
            todo.append((ck, o))
    limits = []
    if w.max_price:
        limits.append(f"price <= {w.max_price:g} RON")
    if w.min_price:
        limits.append(f"price >= {w.min_price:g} RON")
    if w.min_discount:
        limits.append(f"discount >= {w.min_discount:g}%")
    req = _requirements(w) + ("\nPrice rules: " + ", ".join(limits) if limits else "")
    for i in range(0, len(todo), 30):
        chunk = todo[i:i + 30]
        lines = []
        for n, (_, o) in enumerate(chunk):
            old = f", old price {o.old_price:g}" if o.old_price else ""
            lines.append(f"{n}. {o.title} | {o.price:g} RON{old} | shop {o.store} | {o.url}")
        data = _json_call(CLASSIFY_MODEL, GATE_SYSTEM, f"{req}\n\nListings:\n" + "\n".join(lines), GATE_SCHEMA, 8000, "low")
        if not data:
            break   # API trouble: fail open (the rule-based checks already passed these)
        for it in data.get("items", []):
            idx = it.get("i")
            if isinstance(idx, int) and 0 <= idx < len(chunk):
                ck, o = chunk[idx]
                cache[ck] = bool(it.get("ok"))
                out[o.key] = cache[ck]
    return out
