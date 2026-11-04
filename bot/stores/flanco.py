"""Flanco (flanco.ro) - electronics / appliances, Magento 2.

Search: GET https://www.flanco.ro/catalogsearch/result/?q={quote_plus(q)}   page N: &p=N
Cards (standard Magento 2 grid): li.product-item > .product-item-info, a.product-item-link (absolute href
https://www.flanco.ro/{url-key}.html, text = name), .price-box[data-product-id] with
[data-price-type=finalPrice][data-price-amount="2999.99"] and [data-price-type=oldPrice] ("PRP:" label,
EU Omnibus 30-day lowest price). Visible text "2.999,<sup>99</sup> lei"; "Stoc epuizat" = out of stock.
PDP: JSON-LD Product; fallback h1.page-title + .product-info-main .special-price .price / .price.
Anti-bot: Cloudflare (403 / "Just a moment..." reported for plain clients, 25.10.2026) -> BlockedError.
"""
from __future__ import annotations

from ._ro_retail import RoMagentoStore


class FlancoStore(RoMagentoStore):
    key = "flanco"
    name = "Flanco"
    base_url = "https://www.flanco.ro"
    domains = ("flanco.ro",)
    categories = ("electronics", "appliances", "pc", "home")
    page_size = 24
    min_delay = 3.0
    search_path = "/catalogsearch/result/"
    page_param = "p"
    own_names = ("flanco",)
