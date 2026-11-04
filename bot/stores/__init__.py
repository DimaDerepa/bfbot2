"""Store registry: every Store subclass with a `key` in this package is auto-registered."""
from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import Any, Optional
from urllib.parse import urlsplit

from .base import Store
from .platforms import HtmlSearchStore, make_custom_store

log = logging.getLogger("bot.stores")

_SKIP_MODULES = {"base", "platforms"}
_BUILTIN: dict[str, type[Store]] = {}
IMPORT_ERRORS: dict[str, str] = {}
SHOP_ERRORS: dict[str, str] = {}   # settings.shops entries skipped by the last registry() call


def _discover() -> None:
    if _BUILTIN:
        return
    for mod in pkgutil.iter_modules(__path__):  # type: ignore[name-defined]
        if mod.name in _SKIP_MODULES or mod.name.startswith("_"):
            continue
        try:
            m = importlib.import_module(f"{__name__}.{mod.name}")
        except Exception as e:  # a broken adapter must not take the whole bot down
            IMPORT_ERRORS[mod.name] = f"{type(e).__name__}: {e}"
            log.error("store module %s failed to import: %s", mod.name, e)
            continue
        for _, obj in inspect.getmembers(m, inspect.isclass):
            if issubclass(obj, Store) and obj.__module__ == m.__name__ and getattr(obj, "key", ""):
                if obj.key in _BUILTIN:
                    log.warning("duplicate store key %s (%s)", obj.key, obj)
                    continue
                _BUILTIN[obj.key] = obj


def _shop_specs(custom_shops: Any) -> list:
    """settings.shops as a list of entries, whatever shape the (hand-written) YAML gave it."""
    if not custom_shops:
        return []
    if isinstance(custom_shops, dict):
        values = list(custom_shops.values())
        if values and all(isinstance(v, dict) for v in values):   # {myshop: {platform: ..., base_url: ...}, ...}
            return [{**v, "key": v.get("key") or k} for k, v in custom_shops.items()]
        return [custom_shops]                                       # one shop written without the leading "- "
    if isinstance(custom_shops, (list, tuple)):
        return list(custom_shops)
    msg = f"settings.shops: ожидался список магазинов, получено {custom_shops!r}"
    log.error("%s", msg)
    SHOP_ERRORS["shops"] = msg
    return []


def registry(custom_shops: Any = ()) -> dict[str, type[Store]]:
    """Built-in stores plus the custom shops from settings.shops; a bad shop entry is logged and skipped."""
    _discover()
    reg = dict(_BUILTIN)
    SHOP_ERRORS.clear()
    for i, spec in enumerate(_shop_specs(custom_shops)):
        if not isinstance(spec, dict):
            msg = (f"settings.shops[{i}]: магазин нужно описать полями key / platform / base_url "
                   f"(см. README), а получено {spec!r}; магазин пропущен")
            log.error("%s", msg)
            SHOP_ERRORS[f"shops[{i}]"] = msg
            continue
        try:
            cls = make_custom_store(spec)
        except Exception as e:  # a typo in one shop must not stop the whole bot
            msg = str(e) if isinstance(e, ValueError) else f"shop {spec.get('key')!r}: {type(e).__name__}: {e}"
            log.error("%s; магазин пропущен", msg)
            SHOP_ERRORS[str(spec.get("key") or f"shops[{i}]")] = msg
            continue
        if cls.key in _BUILTIN:
            log.warning("custom shop %r replaces the built-in store with the same key", cls.key)
        reg[cls.key] = cls
    return reg


def store_for_url(url: str, reg: dict[str, type[Store]]) -> Optional[type[Store]]:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    for cls in reg.values():
        for d in cls.domains or (urlsplit(cls.base_url).netloc.lower().removeprefix("www."),):
            d = d.lower().removeprefix("www.")
            if d and (host == d or host.endswith("." + d)):
                return cls
    return None


def web_store_for(url: str) -> type[Store]:
    """Ad-hoc store for a product URL on a shop we have no adapter for."""
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    key = "web:" + host
    return type(f"Web_{host}", (Store,), {
        "key": key, "name": host, "base_url": f"https://{host}", "domains": (host,), "categories": ("general",),
    })


__all__ = ["Store", "HtmlSearchStore", "registry", "store_for_url", "web_store_for", "IMPORT_ERRORS",
           "SHOP_ERRORS"]
