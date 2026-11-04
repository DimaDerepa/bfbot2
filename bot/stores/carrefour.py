"""Carrefour Romania (carrefour.ro, no www) - hypermarket + marketplace, Magento 2 (customised theme).

Search: GET https://carrefour.ro/catalogsearch/result/?q={quote_plus(q)}   page N: &p=N
        (SEO alternative /produse/{q}; robots.txt reportedly disallows search pages -> keep frequency low)
Cards (standard Magento selectors, unverified for this theme - diagnostics will tell): li.product-item,
a.product-item-link, [data-price-type=finalPrice|oldPrice][data-price-amount]. Visible text renders the
decimals in a separate element, so plain text reads "2.599 99 Lei" (= 2599.99) - handled by price_text.
Product URL https://carrefour.ro/produse/{slug}-{sku} with sku like "19-44502726" (stable id).
Seller line: "Supermarket Online" / Carrefour (own) vs marketplace partners (-> Offer.seller).
"""
from __future__ import annotations

import re

from ._ro_retail import RoMagentoStore


class CarrefourStore(RoMagentoStore):
    key = "carrefour"
    name = "Carrefour"
    base_url = "https://carrefour.ro"
    domains = ("carrefour.ro",)
    categories = ("general", "electronics", "appliances", "home", "pc", "toys")
    page_size = 24
    min_delay = 3.0
    search_path = "/catalogsearch/result/"
    page_param = "p"
    id_re = re.compile(r"[/-](\d{1,3}-\d{5,})/?$")
    with_seller = True
    own_names = ("carrefour", "supermarket online")
