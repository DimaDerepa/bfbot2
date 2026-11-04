from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


def canonical_url(url: str) -> str:
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower() or "https", p.netloc.lower().removeprefix("www."), path, "", ""))


@dataclass
class Offer:
    store: str                      # store key, e.g. "emag"
    title: str
    url: str
    price: float                    # current price, RON
    old_price: Optional[float] = None   # list / PRP / "preț vechi" price shown by the store
    currency: str = "RON"
    in_stock: Optional[bool] = None     # None = unknown
    offer_id: Optional[str] = None      # stable id inside the store (sku / product id)
    image: Optional[str] = None
    seller: Optional[str] = None        # marketplace seller, or cheapest shop for aggregators
    shop_count: Optional[int] = None    # aggregators: how many shops sell it
    store_name: Optional[str] = None    # display name (custom shops)
    source: str = ""                    # extraction strategy, for diagnostics

    @property
    def key(self) -> str:
        return f"{self.store}::{self.offer_id or canonical_url(self.url)}"

    @property
    def discount_pct(self) -> Optional[float]:
        if self.old_price and self.price and self.old_price > self.price:
            return round((self.old_price - self.price) / self.old_price * 100, 1)
        return None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Watch:
    name: str
    query: Optional[str] = None
    urls: list[str] = field(default_factory=list)
    must_include: list[str] = field(default_factory=list)
    any_of: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_discount: Optional[float] = None      # percent
    in_stock: bool = True                     # skip offers known to be out of stock
    stores: Optional[list[str]] = None        # None = default store set
    skip_stores: list[str] = field(default_factory=list)
    categories: Optional[list[str]] = None
    strict: bool = True                       # all query words must be in title (when must_include empty)
    exclude_accessories: bool = True
    drop_pct: Optional[float] = None          # re-alert when price drops by >= this % (default from settings)
    enabled: bool = True
    ram_min: Optional[int] = None             # parameter search: RAM >= N GB (read from the title)
    year_min: Optional[int] = None            # parameter search: model released in year >= N (model table)
    allow_unknown_specs: bool = False         # let offers through when RAM/year cannot be determined
    pages: Optional[int] = None               # result pages per shop (parameter searches look deeper)
    origin: str = "config"                    # "config" | "telegram"
    id: str = ""

    def __post_init__(self) -> None:
        from .text import slug

        for f in ("ram_min", "year_min", "pages"):
            v = getattr(self, f)
            if v in ("", 0):
                setattr(self, f, None)
            elif v is not None:
                setattr(self, f, int(float(v)))
        if not self.id:
            self.id = slug(self.name)
        if self.query is None and not self.urls:
            raise ValueError(f"watch '{self.name}': need 'query' or 'urls'")

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        """Short human readable criteria summary (Russian)."""
        parts: list[str] = []
        if self.query:
            parts.append(f"«{self.query}»")
        if self.urls:
            parts.append(f"{len(self.urls)} ссылк(и)")
        if self.min_price is not None and self.max_price is not None:
            parts.append(f"{self.min_price:g}–{self.max_price:g} лей")
        elif self.max_price is not None:
            parts.append(f"до {self.max_price:g} лей")
        elif self.min_price is not None:
            parts.append(f"от {self.min_price:g} лей")
        if self.min_discount:
            parts.append(f"скидка ≥{self.min_discount:g}%")
        if self.ram_min:
            parts.append(f"ОЗУ ≥{self.ram_min} ГБ")
        if self.year_min:
            parts.append(f"модель {self.year_min}+")
        if self.must_include:
            parts.append("+" + " +".join(self.must_include))
        if self.any_of:
            parts.append("одно из: " + ", ".join(self.any_of))
        if self.exclude:
            parts.append("-" + " -".join(self.exclude))
        if self.stores:
            parts.append("@" + ",".join(self.stores))
        if self.categories:
            parts.append("#" + ",".join(self.categories))
        if not self.in_stock:
            parts.append("включая нет в наличии")
        return " · ".join(parts)
