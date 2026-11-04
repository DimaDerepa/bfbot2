import os

import pytest

from bot.net import BlockedError, Response
from bot.stores.shopmania import ShopmaniaStore, shopmania_id
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "shopmania_search.html"), encoding="utf-8").read()


def test_shopmania_search_url_slug():
    http = FakeHttp({"shopmania.ro/q-": HTML})
    make_store(ShopmaniaStore, http).search("Căști Sony WH-1000XM5 (negru)")
    assert http.calls == ["https://www.shopmania.ro/q-casti-sony-wh-1000xm5-negru"]


def test_shopmania_falls_back_to_shopping_route_on_404():
    http = FakeHttp({"shopmania.ro/shopping?q=": HTML})       # /q-... has no route -> 404
    offers = make_store(ShopmaniaStore, http).search("iphone 15")
    assert http.calls == ["https://www.shopmania.ro/q-iphone-15", "https://www.shopmania.ro/shopping?q=iphone%2015"]
    assert len(offers) == 4


def test_shopmania_cards():
    http = FakeHttp({"shopmania.ro/q-iphone-15": HTML})
    store = make_store(ShopmaniaStore, http)
    offers = store.search("iphone 15")
    assert store.last_strategy == "cards"
    by = {o.offer_id: o for o in offers}
    assert len(offers) == 4                                     # card without price skipped
    a = by["540226153"]
    assert a.title == "Apple iPhone 15 128GB"
    assert a.url == "https://www.shopmania.ro/telefoane-mobile/p-apple-iphone-15-128gb-540226153"   # no utm params
    assert a.price == 2896.0 and a.old_price is None and a.shop_count == 37 and a.in_stock is None
    assert a.image == "https://s.cdnshm.com/catalog/ro/t/540226153/apple-iphone-15-128gb.jpg"      # not na.svg
    s = by["561396316"]
    assert s.price == 3149.0 and s.old_price == 4299.0 and s.shop_count == 12
    assert s.image == "https://www.shopmania.ro/catalog/ro/t/561396316/samsung-galaxy-s24-256gb.jpg"
    r = by["t-husa-silicon-iphone-15-transparenta"]             # single-shop item behind /redirect?d=
    assert r.url == "https://www.shopmania.ro/redirect?d=aHR0cHM6Ly9nc21uZXQucm8vaHVzYS0xNQ"   # s/pos dropped
    assert r.price == 29.99 and r.seller == "gsmnet.ro"          # shipping "de la 15,00 RON" ignored
    assert r.shop_count == 1 and r.image == "https://s.cdnshm.com/catalog/ro/t/99/husa-iphone-15.jpg"
    o = by["540226999"]
    assert o.price == 7499.0 and o.in_stock is False
    assert store._pager_get(2) == "https://www.shopmania.ro/q-iphone-15/p2"


def test_shopmania_link_fallback_when_layout_changes():
    html = ('<html><body><ul>'
            '<li><a href="/televizoare/p-samsung-qe55q80d-55-qled-4k-611223344">Samsung QE55Q80D</a>'
            '<span>Vezi 8 oferte, de la 3.499,00 RON</span></li>'
            '<li><a href="/televizoare/p-lg-oled55c4-611223355">LG OLED55C4</a><b>5.199,99 RON</b></li>'
            '<li><a href="/televizoare">Televizoare</a> 1.000,00 RON</li>'
            '</ul></body></html>')
    store = make_store(ShopmaniaStore, FakeHttp({"shopmania.ro/q-": html}))
    offers = store.search("tv")
    assert store.last_strategy == "product-links"
    by = {o.offer_id: o for o in offers}
    assert set(by) == {"611223344", "611223355"}
    assert by["611223344"].price == 3499.0 and by["611223344"].shop_count == 8
    assert by["611223355"].price == 5199.99 and by["611223355"].title == "LG OLED55C4"


OOPS = '<html><body><h2 class="h4 serif mb-2">Ooops... ☹️</h2><p>Nu am găsit rezultate.</p></body></html>'


def test_shopmania_empty_result_page():
    http = FakeHttp({"shopmania.ro/": OOPS})
    store = make_store(ShopmaniaStore, http)
    assert store.search("xyz xyz") == [] and store.last_strategy == "no-results"
    # no SEO landing page for this query -> the site search is asked too, then we stop (no generic fallback junk)
    assert http.calls == ["https://www.shopmania.ro/q-xyz-xyz", "https://www.shopmania.ro/shopping?q=xyz%20xyz"]


def test_shopmania_ooops_seo_page_falls_back_to_site_search():
    http = FakeHttp({"shopmania.ro/q-": OOPS, "shopmania.ro/shopping?q=": HTML})
    store = make_store(ShopmaniaStore, http)
    offers = store.search("iphone 15")
    assert http.calls == ["https://www.shopmania.ro/q-iphone-15", "https://www.shopmania.ro/shopping?q=iphone%2015"]
    assert len(offers) == 4 and store.last_strategy == "cards"


class _RedirectHttp(FakeHttp):
    """Serves the routed body but reports `final` as the URL after redirects."""

    def __init__(self, routes, final):
        super().__init__(routes)
        self.final = final

    def request(self, method, url, **kw):
        r = super().request(method, url, **kw)
        return Response(url=self.final, status=r.status, text=r.text, headers={})


def test_shopmania_redirect_to_home_page_is_not_a_result_list():
    http = _RedirectHttp({"shopmania.ro/": HTML}, "https://www.shopmania.ro/")       # home has promo .prod-item cards
    store = make_store(ShopmaniaStore, http)
    assert store.search("iphone 15") == [] and store.last_strategy == "redirect-home"
    assert http.calls[1] == "https://www.shopmania.ro/shopping?q=iphone%2015"     # /q- went home -> site search


def test_shopmania_search_landing_on_product_page():
    url = "https://www.shopmania.ro/telefoane-mobile/p-apple-iphone-15-128gb-540226153"
    html = ('<html><body><h1>Apple iPhone 15 128GB</h1><div class="offers-head">Vezi 37 oferte, de la 2.896,00 RON'
            '</div></body></html>')
    store = make_store(ShopmaniaStore, _RedirectHttp({"shopmania.ro/q-": html}, url + "?ref=q"))
    (o,) = store.search("apple iphone 15 128gb")
    assert store.last_strategy == "product-page"
    assert o.url == url and o.offer_id == "540226153" and o.price == 2896.0 and o.shop_count == 37


def test_shopmania_product_page_header_text():
    url = "https://www.shopmania.ro/telefoane-mobile/p-apple-iphone-15-128gb-540226153"
    html = ('<html><body><h1>Apple iPhone 15 128GB</h1><div class="offers-head">Vezi 37 oferte, de la 2.896,00 RON'
            '</div><div>Cost livrare de la 28,00 RON</div></body></html>')
    o = make_store(ShopmaniaStore, FakeHttp({"p-apple-iphone-15-128gb": html})).product(url)
    assert o.price == 2896.0 and o.shop_count == 37 and o.offer_id == "540226153" and o.url == url


def test_shopmania_product_page_header_min_beats_first_microdata_offer():
    # the first itemprop=price on a comparator page is just the first listed shop, not the cheapest
    url = "https://www.shopmania.ro/telefoane-mobile/p-apple-iphone-15-128gb-540226153"
    html = ('<html><body><h1>Apple iPhone 15 128GB</h1><div class="offers-head">Vezi 37 oferte, de la 2.896,00 RON'
            '</div><div itemscope itemtype="http://schema.org/Product"><span itemprop="name">Apple iPhone 15 128GB</span>'
            '<span itemprop="price" content="3299.00">3.299,00 RON</span></div></body></html>')
    o = make_store(ShopmaniaStore, FakeHttp({"p-apple-iphone-15-128gb": html})).product(url)
    assert o.price == 2896.0 and o.shop_count == 37 and o.source == "shopmania-pdp"


def test_shopmania_product_page_jsonld_aggregate_offer():
    url = "https://www.shopmania.ro/telefoane-mobile/p-samsung-galaxy-s24-256gb-561396316"
    html = ('<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",'
            '"name":"Samsung Galaxy S24 256GB","offers":{"@type":"AggregateOffer","lowPrice":"3149.00",'
            '"highPrice":"4299.00","offerCount":"12","priceCurrency":"RON"}}</script></head>'
            '<body><h1>Samsung Galaxy S24 256GB</h1></body></html>')
    o = make_store(ShopmaniaStore, FakeHttp({"p-samsung-galaxy-s24": html})).product(url)
    assert o.price == 3149.0 and o.old_price is None and o.shop_count == 12 and o.offer_id == "561396316"


def test_shopmania_blocked():
    with pytest.raises(BlockedError):
        make_store(ShopmaniaStore, FakeHttp({"shopmania.ro": (429, "Too many requests")})).search("tv")


def test_shopmania_id():
    assert shopmania_id("https://www.shopmania.ro/telefoane-mobile/p-samsung-galaxy-s24-256gb-561396316") == "561396316"
    assert shopmania_id("https://www.shopmania.ro/telefoane-mobile/samsung") is None
