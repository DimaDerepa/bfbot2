"""Reserved Romania (reserved.com/ro/ro/) - LPP group fashion; same platform as Sinsay (see sinsay.py).

DISABLED BY DEFAULT for the same reasons: client-side Algolia search with unknown credentials, browser needed.
Search (unverified, same pattern as Sinsay): GET https://www.reserved.com/ro/ro/?query={quote(q)}
Product images live at https://www.reserved.com/media/catalog/product/... (Magento-style media path).
"""
from __future__ import annotations

from .sinsay import SinsayStore


class ReservedStore(SinsayStore):
    key = "reserved"
    name = "Reserved"
    base_url = "https://www.reserved.com"
    locale_path = "/ro/ro/"
    domains = ("reserved.com",)
    categories = ("fashion",)
    enabled_by_default = False
    extra_headers = {"Referer": "https://www.reserved.com/ro/ro/", "Upgrade-Insecure-Requests": "1"}
