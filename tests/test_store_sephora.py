import os

import pytest

from bot.net import BlockedError
from bot.stores.sephora import SephoraStore, sephora_pid
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HTML = open(os.path.join(FIX, "sephora_search.html"), encoding="utf-8").read()
PDP = open(os.path.join(FIX, "sephora_product.html"), encoding="utf-8").read()
SEARCH = "https://www.sephora.ro/on/demandware.store/Sites-Sephora_RO-Site/ro_RO/Search-Show"


def test_sephora_tcproduct_tiles():
    http = FakeHttp({"Search-Show": HTML})
    store = make_store(SephoraStore, http)
    offers = store.search("parfum")
    assert http.calls == [SEARCH + "?q=parfum&sz=48&start=0"]
    by = {o.offer_id: o for o in offers}
    assert list(by) == ["128302", "P569004", "776622", "P1000045"]          # broken-JSON tile without a name dropped
    f = by["128302"]
    assert f.title == "VIKTOR&ROLF Flowerbomb - Eau de Parfum 50 ml"
    assert f.url == "https://www.sephora.ro/p/flowerbomb---eau-de-parfum-128302.html"
    assert f.price == 629.0 and f.old_price is None                      # old == current -> no promo
    assert f.image == "https://www.sephora.ro/dw/image/v2/BCZG_PRD/128302_50.jpg?sw=300"
    e = by["P569004"]
    assert e.title == "ELIE SAAB Le Parfum - Eau de Parfum"
    assert e.price == 1299.0 and e.old_price == 1529.0
    assert e.image.startswith("https://www.sephora.ro/dw/image/v2/")    # protocol-relative picture fixed
    s = by["776622"]
    assert s.price == 489.0 and s.in_stock is False                     # tc price 0 -> visible price
    lan = by["P1000045"]                                                # tile without data-tcproduct
    assert lan.title == "LANCÔME Advanced Génifique - Ser"
    assert lan.url == "https://www.sephora.ro/p/advanced-genifique---ser-P1000045.html"
    assert lan.price == 524.5 and lan.old_price is None                 # "1.049,00 Lei / 100 ml" is a unit price
    assert lan.image == "https://www.sephora.ro/dw/image/P1000045.jpg"


def test_sephora_query_encoding_and_paging():
    store = make_store(SephoraStore, FakeHttp({}))
    assert store.search_url("dior sauvage", 1) == SEARCH + "?q=dior+sauvage&sz=48&start=0"
    assert store.search_url("dior sauvage", 3) == SEARCH + "?q=dior+sauvage&sz=48&start=96"
    assert store.search_url("l'oréal", 1) == SEARCH + "?q=l%27or%C3%A9al&sz=48&start=0"


def test_sephora_mobile_headers_and_pacing():
    h = SephoraStore.extra_headers
    assert "Mobile" in h["User-Agent"] and "Android" in h["User-Agent"]
    assert h["sec-ch-ua-mobile"] == "?1" and SephoraStore.min_delay >= 8


def test_sephora_product_page():
    url = "https://www.sephora.ro/p/le-parfum---eau-de-parfum-P569004.html"
    o = make_store(SephoraStore, FakeHttp({"sephora.ro/p/": PDP})).product(url)
    assert o.title == "ELIE SAAB Le Parfum - Eau de Parfum"
    assert o.price == 1299.0 and o.old_price == 1529.0 and o.in_stock is True
    assert o.offer_id == "P569004" and o.url == url and o.store_name == "Sephora"


def test_sephora_akamai_block():
    with pytest.raises(BlockedError):
        make_store(SephoraStore, FakeHttp({"sephora.ro": (403, "<html><title>Access Denied</title></html>")})).search("x")
    page = "<html><head><title>Sephora</title></head><body><script src='https://ct.captcha-delivery.com/c.js'></script></body></html>"
    with pytest.raises(BlockedError):
        make_store(SephoraStore, FakeHttp({"sephora.ro": page})).search("x")


def test_sephora_pid():
    assert sephora_pid("https://www.sephora.ro/p/flowerbomb---eau-de-parfum-128302.html") == "128302"
    assert sephora_pid("https://www.sephora.ro/p/le-parfum---eau-de-parfum-P569004.html") == "P569004"
    assert sephora_pid("https://www.sephora.ro/parfum/") is None
