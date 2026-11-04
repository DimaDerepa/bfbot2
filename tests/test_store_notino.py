import os

import pytest

from bot.net import BlockedError
from bot.stores.notino import NotinoStore, apollo_state, notino_variant
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HTML = open(os.path.join(FIX, "notino_search.html"), encoding="utf-8").read()
PDP = open(os.path.join(FIX, "notino_product.html"), encoding="utf-8").read()


def test_notino_listing_cards():
    http = FakeHttp({"notino.ro/search.asp": HTML})
    store = make_store(NotinoStore, http, max_pages=3)
    offers = store.search("dior sauvage")
    assert http.calls == ["https://www.notino.ro/search.asp?exps=dior%20sauvage"]          # one page only
    assert store.last_strategy == "cards"
    assert len(offers) == 3                                            # no-price card and promo tile dropped
    d, cg, nv = offers
    assert d.title == "Dior Sauvage Eau de Parfum pentru bărbați"
    assert d.url == "https://www.notino.ro/dior/sauvage-eau-de-parfum-pentru-barbati/p-16107233/"
    assert d.offer_id == "16107233"
    assert d.price == 1299.9
    assert d.old_price == 1349.0              # Omnibus 30-day low preferred to the struck 1.449,00
    assert d.image.startswith("https://cdn.notinoimg.com/")
    assert cg.title == "Carolina Herrera Good Girl Eau de Parfum pentru femei"
    assert cg.price == 679.0 and cg.old_price is None                   # "13,58 lei / 1 ml" unit price ignored
    assert cg.offer_id is None and cg.url == "https://www.notino.ro/carolina-herrera/good-girl-eau-de-parfum-pentru-femei/"
    assert nv.price == 89.9 and nv.in_stock is False and nv.offer_id == "15895673"
    assert d.in_stock is None and cg.in_stock is None


def test_notino_product_page_apollo():
    http = FakeHttp({"notino.ro/dior/": PDP})
    store = make_store(NotinoStore, http)
    base = "https://www.notino.ro/dior/sauvage-eau-de-parfum-pentru-barbati/"
    o = store.product(base)                              # no variant in URL -> input[name=productId] = 16107233
    assert o.offer_id == "16107233" and o.url == base
    assert o.title == "Dior Sauvage Eau de Parfum pentru bărbați 100 ml"
    assert o.price == 649.0 and o.old_price == 699.0 and o.in_stock is True   # price < recentMin < original
    v = store.product(base + "p-16107234/")              # Black-Friday voucher price
    assert v.offer_id == "16107234" and v.price == 809.1 and v.old_price == 899.0
    c = store.product(base + "p-16107235/")              # conditional (price-based) voucher, not buyable
    assert c.price == 1424.05 and c.old_price == 1499.0 and c.in_stock is False


def test_notino_search_landing_on_product_page():
    offers = make_store(NotinoStore, FakeHttp({"notino.ro/search.asp": PDP})).search("sauvage")
    assert sorted(o.offer_id for o in offers) == ["16107233", "16107234", "16107235"]
    assert all(o.url.startswith("https://www.notino.ro/dior/sauvage-eau-de-parfum-pentru-barbati/p-") for o in offers)


def test_notino_cloudflare_challenge():
    page = '<html><head><title>Un moment…</title></head><body><script>window._cf_chl_opt={cvId:"3"}</script></body></html>'
    with pytest.raises(BlockedError):
        make_store(NotinoStore, FakeHttp({"notino.ro": page})).search("parfum")


def test_notino_helpers():
    assert notino_variant("https://www.notino.ro/dior/sauvage/p-123456/") == "123456"
    assert notino_variant("https://www.notino.ro/dior/sauvage/") is None
    assert apollo_state("<script>window.__APOLLO_STATE__ = {\"a\": 1};</script>") == {"a": 1}
    assert apollo_state("<html></html>") == {}
