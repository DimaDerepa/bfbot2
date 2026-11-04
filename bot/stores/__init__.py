"""Store registry: every Store subclass with a `key` in this package is auto-registered."""
from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .base import Store
from .platforms import HtmlSearchStore, make_custom_store

log = logging.getLogger("bot.stores")

_SKIP_MODULES = {"base", "platforms"}
_BUILTIN: dict[str, type[Store]] = {}
IMPORT_ERRORS: dict[str, str] = {}


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


def registry(custom_shops: Iterable[dict] = ()) -> dict[str, type[Store]]:
    _discover()
    reg = dict(_BUILTIN)
    for spec in custom_shops or ():
        try:
            cls = make_custom_store(spec)
        except ValueError as e:
            log.error("%s", e)
            continue
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


__all__ = ["Store", "HtmlSearchStore", "registry", "store_for_url", "web_store_for", "IMPORT_ERRORS"]
