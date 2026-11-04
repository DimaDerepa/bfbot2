import os

import pytest

from bot.net import BlockedError
from bot.stores.fashiondays import FashionDaysStore, fd_offer_id
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "fashiondays_search.html"), encoding="utf-8").read()


def test_fashiondays_gtm_cards():
    http = FakeHttp({"fashiondays.ro/search/": HTML})
    store = make_store(FashionDaysStore, http)
    offers = store.search("adidas samba")
    assert http.calls == ["https://www.fashiondays.ro/search/?q=adidas%20samba"]
    assert store.last_strategy == "gtm-cards"
    by = {o.offer_id: o for o in offers}
    # banner (no /p/ link), duplicate card and the card without a price are dropped
    assert list(by) == ["2547171-2", "3301876", "5234213-2", "4410022"]
    a = by["2547171-2"]
    assert a.title == "adidas Originals Pantofi sport de piele Samba OG"
    assert a.url == "https://www.fashiondays.ro/p/pantofi-sport-de-piele-samba-og-adidas-originals-p2547171-2/"
    assert a.price == 449.99 and a.old_price == 549.99
    assert a.in_stock is True                                     # limited_quantity
    assert a.image == "https://fdcdn.akamaized.net/Files/7/S/320x467/2547171-1.jpg"
    assert a.store_name == "Fashion Days"
    g = by["3301876"]
    assert g.price == 1299.99 and g.old_price == 1599.99          # thousands separator; voucher line ignored
    levis = by["5234213-2"]
    assert levis.title == "Levi's Blugi drepti cu aspect decolorat 501"
    assert levis.price == 389.99 and levis.old_price is None and levis.in_stock is True
    sold = by["4410022"]
    assert sold.price == 599.0 and sold.in_stock is False


def test_fashiondays_unmarked_rrp_and_title_fallbacks():
    html = """<ul><li class="product-card"><a href="/p/rochie-midi-guess-p7001234/?gtm_data=x" data-gtm-price="349.5"
      data-gtm-status=""><span class="product-card-brand">GUESS</span><span class="product-card-title">Rochie midi</span>
      <div class="product-card-price"><span>499,00 Lei</span><span>349,50 Lei</span>
      <span>12,50 Lei / buc</span></div></a></li></ul>"""
    offers = make_store(FashionDaysStore, FakeHttp({"fashiondays.ro/search/": html})).search("guess rochie")
    assert len(offers) == 1
    o = offers[0]
    assert o.title == "GUESS Rochie midi" and o.url == "https://www.fashiondays.ro/p/rochie-midi-guess-p7001234/"
    assert o.price == 349.5 and o.old_price == 499.0 and o.in_stock is None and o.offer_id == "7001234"


def test_fashiondays_query_encoding_and_single_page():
    http = FakeHttp({"fashiondays.ro/search/": HTML})
    store = make_store(FashionDaysStore, http, max_pages=3)
    store.search("Nike Air Force 1 '07")
    assert http.calls == ["https://www.fashiondays.ro/search/?q=Nike%20Air%20Force%201%20%2707"]


def test_fashiondays_redirect_home_and_challenge():
    http = FakeHttp({"fashiondays.ro/search/": "<html><title>Fashion Days</title><body>home</body></html>"})
    store = make_store(FashionDaysStore, http)
    store.http.request = _redirect_to(http, "https://www.fashiondays.ro/")
    assert store.search("x") == [] and store.last_strategy == "redirect:home"
    blocked = FakeHttp({"fashiondays.ro": "<html><head><title>Access Denied</title></head><body>Reference #18</body></html>"})
    with pytest.raises(BlockedError):
        make_store(FashionDaysStore, blocked).search("x")


def test_fashiondays_offer_id():
    assert fd_offer_id("https://www.fashiondays.ro/p/x-p123456/") == "123456"
    assert fd_offer_id("https://www.fashiondays.ro/p/x-p123456-3/") == "123456-3"
    assert fd_offer_id("https://www.fashiondays.ro/campanii/x/") is None


def _redirect_to(http, final):
    orig = http.request

    def request(method, url, **kw):
        resp = orig(method, url, **kw)
        resp.url = final
        return resp
    return request


FD_PDP = """<html><head><title>Pantofi sport de piele Samba OG - adidas Originals | FashionDays.ro</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"adidas Originals Pantofi sport de piele Samba OG",
 "sku":"2547171-2","offers":{"@type":"Offer","price":"449.99","priceCurrency":"RON","availability":"https://schema.org/InStock"}}</script>
</head><body><h1>Pantofi sport de piele Samba OG</h1>
<ul class="similar-products"><li class="product-card"><a href="https://www.fashiondays.ro/p/tricou-p5512098/?gtm_data=x"
  data-gtm-price="99.99" data-gtm-name="Tricou cu logo" data-gtm-brand-name="adidas Originals"></a></li></ul></body></html>"""


def test_fashiondays_search_redirected_to_product_page():
    http = FakeHttp({"fashiondays.ro/search/": FD_PDP})
    store = make_store(FashionDaysStore, http)
    store.http.request = _redirect_to(http, "https://www.fashiondays.ro/p/pantofi-sport-de-piele-samba-og-adidas-originals-p2547171-2/")
    offers = store.search("samba og")
    assert store.last_strategy == "product-page"
    assert len(offers) == 1                                  # the "similar products" card is not returned
    o = offers[0]
    assert o.price == 449.99 and o.in_stock is True and o.offer_id == "2547171-2"
    assert o.url == "https://www.fashiondays.ro/p/pantofi-sport-de-piele-samba-og-adidas-originals-p2547171-2/"


def test_fashiondays_omnibus_label_beats_rrp():
    html = """<ul><li class="product-card"><a href="/p/geaca-puma-p6100001/" data-gtm-price="299.99"
      data-gtm-name="Geaca matlasata" data-gtm-brand-name="Puma" data-gtm-status="in_stock">
      <div class="product-card-price"><span>PRP: 449,99 Lei</span><span class="product-card-price-new">299,99 Lei</span>
      <span>Cel mai mic preț din ultimele 30 de zile:</span><span>349,99 Lei</span></div></a></li></ul>"""
    o = make_store(FashionDaysStore, FakeHttp({"fashiondays.ro/search/": html})).search("geaca puma")[0]
    assert o.title == "Puma Geaca matlasata" and o.price == 299.99
    assert o.old_price == 349.99                             # EU 30-day low, not the 449,99 RRP
