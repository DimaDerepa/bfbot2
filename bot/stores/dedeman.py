"""Dedeman (dedeman.ro) - DIY / home improvement, Magento 2 markup.

Search: GET https://www.dedeman.ro/ro/catalogsearch/result/v2?q={quote_plus(q)}&per_page=96
        (note the '/v2' segment; page N: &page=N - parameter name unconfirmed, one page of 96 is the norm)
Cards: a.product-item-link (title + href /ro/{slug}/p/{sku}), span.price-container.price-final_price with
[data-price-type=finalPrice][data-price-amount="279"] ("Super Pret" when discounted) and
span.old-price [data-price-type=oldPrice] (regular price). Visible text "298.00 lei /bucata" (dot decimal)
- data-price-amount is preferred. Stable id = the number after /p/.
Anti-bot: none reported (plain fetches work); throttled to one request every ~3 s.
"""
from __future__ import annotations

import re

from ._ro_retail import RoMagentoStore


class DedemanStore(RoMagentoStore):
    key = "dedeman"
    name = "Dedeman"
    base_url = "https://www.dedeman.ro"
    domains = ("dedeman.ro",)
    categories = ("diy", "home", "appliances")
    page_size = 96
    min_delay = 3.0
    search_path = "/ro/catalogsearch/result/v2"
    extra_params = {"per_page": "96"}
    page_param = "page"
    id_re = re.compile(r"/p/(\d+)/?$")
    own_names = ("dedeman",)
