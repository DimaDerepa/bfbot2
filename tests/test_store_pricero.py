import os

import pytest

from bot.net import BlockedError, Response
from bot.stores.pricero import PriceRoStore, pricero_id, pricero_slug
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "pricero_search.html"), encoding="utf-8").read()


def test_pricero_slug_splits_digit_letter_boundaries():
    assert pricero_slug("Apple iPhone 15 Pro 256GB") == "apple-iphone-15-pro-256-gb"
    assert pricero_slug("Samsung Galaxy S24 5G") == "samsung-galaxy-s24-5-g"
    assert pricero_slug("Mașină de spălat Bosch") == "masina-de-spalat-bosch"


def test_pricero_search_url():
    http = FakeHttp({"price.ro/rezultate~": HTML})
    make_store(PriceRoStore, http).search("Apple iPhone 15 Pro 256GB")
    assert http.calls == ["https://www.price.ro/rezultate~apple-iphone-15-pro-256-gb.html"]


def test_pricero_legacy_route_on_404():
    http = FakeHttp({"price.ro/index.php?action=q": HTML})
    offers = make_store(PriceRoStore, http).search("apple iphone 15 pro 256gb")
    assert http.calls == ["https://www.price.ro/rezultate~apple-iphone-15-pro-256-gb.html",
                          "https://www.price.ro/index.php?action=q&text=apple+iphone+15+pro+256gb&submit=Cauta"]
    assert len(offers) == 3


def test_pricero_listing_from_product_links():
    http = FakeHttp({"price.ro/rezultate~": HTML})
    store = make_store(PriceRoStore, http)
    offers = store.search("apple iphone 15 pro 256gb")
    assert store.last_strategy == "product-links"
    by = {o.offer_id: o for o in offers}
    assert set(by) == {"4336871", "4336874", "4336877"}          # product without a price skipped
    a = by["4336871"]
    assert a.title == "Apple iPhone 15 Pro 256GB Black Titanium"
    assert a.url == "https://www.price.ro/preturi-apple-iphone-15-pro-256gb-black-titanium-4336871"
    assert a.price == 5920.0 and a.shop_count == 14             # "Rate de la 247 lei/lună" ignored
    assert a.image == "https://www.price.ro/imagini/4336871.jpg"
    assert by["4336874"].price == 3967.99 and by["4336874"].shop_count == 2
    assert by["4336877"].price == 9799.0 and by["4336877"].shop_count == 1
    assert all(o.old_price is None and o.in_stock is None for o in offers)
    assert store._pager_get(2) == "https://www.price.ro/rezultate~apple-iphone-15-pro-256-gb.html?pagina=2"


def test_pricero_jsonld_listing():
    html = ('<html><head><script type="application/ld+json">{"@type":"ItemList","itemListElement":['
            '{"@type":"ListItem","position":1,"item":{"@type":"Product","name":"Laptop ASUS Vivobook 15 X1504VA",'
            '"url":"https://www.price.ro/preturi-laptop-asus-vivobook-15-x1504va-5100201?utm_source=x",'
            '"offers":{"@type":"AggregateOffer","lowPrice":"2.199,99","offerCount":5,"priceCurrency":"RON"}}},'
            '{"@type":"ListItem","position":2,"item":{"@type":"Product","name":"Laptop Lenovo IdeaPad Slim 3",'
            '"url":"/preturi-laptop-lenovo-ideapad-slim-3-5100202","offers":{"@type":"AggregateOffer",'
            '"lowPrice":1899,"offerCount":3,"priceCurrency":"RON"}}}]}</script></head><body></body></html>')
    store = make_store(PriceRoStore, FakeHttp({"price.ro/rezultate~": html}))
    offers = store.search("laptop")
    assert store.last_strategy == "jsonld"
    a, b = offers
    assert a.offer_id == "5100201" and a.price == 2199.99 and a.shop_count == 5
    assert a.url == "https://www.price.ro/preturi-laptop-asus-vivobook-15-x1504va-5100201"
    assert b.offer_id == "5100202" and b.price == 1899.0


def test_pricero_cloudflare_403_is_blocked():
    http = FakeHttp({"price.ro": (403, "<html><head><title>Just a moment...</title></head></html>")})
    with pytest.raises(BlockedError):
        make_store(PriceRoStore, http).search("iphone")


def test_pricero_product_page_text_fallback():
    url = "https://www.price.ro/preturi-apple-iphone-15-pro-1tb-black-titanium-4336877"
    html = ('<html><body><h1>Apple iPhone 15 Pro 1TB Black Titanium</h1><div class="sumar">Cel mai mic preț: '
            'de la 9.799 lei în 6 magazine</div><div id="footer">peste 1500 magazine</div></body></html>')
    o = make_store(PriceRoStore, FakeHttp({"preturi-apple-iphone-15-pro-1tb": html})).product(url)
    assert o.price == 9799.0 and o.shop_count == 6 and o.offer_id == "4336877"


def test_pricero_product_page_ignores_instalments_and_first_listed_shop():
    url = "https://www.price.ro/preturi-apple-iphone-15-pro-1tb-black-titanium-4336877"
    html = ('<html><body><h1>Apple iPhone 15 Pro 1TB Black Titanium</h1>'
            '<div class="rate">Rate de la 409 lei</div>'                               # instalment, shown first
            '<div class="sumar">de la 9.799 lei în 6 magazine</div>'
            '<div itemscope itemtype="http://schema.org/Product"><span itemprop="name">Apple iPhone 15 Pro 1TB</span>'
            '<span itemprop="price" content="10499.00">10.499 lei</span></div>'        # first shop row, not the min
            '</body></html>')
    o = make_store(PriceRoStore, FakeHttp({"preturi-apple-iphone-15-pro-1tb": html})).product(url)
    assert o.price == 9799.0 and o.shop_count == 6 and o.source == "pricero-pdp"


def test_pricero_card_instalment_before_min_price():
    html = ('<html><body><div class="lista">'
            '<div class="produs"><h2><a href="/preturi-samsung-galaxy-s25-ultra-512gb-5300001">Samsung Galaxy S25 Ultra'
            ' 512GB</a></h2><span>Rate lunare de la 312 lei</span> <span>de la 6.499,99 lei</span> <span>9 magazine</span>'
            '</div><div class="produs"><h2><a href="/preturi-samsung-galaxy-s25-256gb-5300002">Samsung Galaxy S25 256GB'
            '</a></h2><span>de la 3.899 lei</span> <span>Transport 19,99 lei</span></div></div></body></html>')
    by = {o.offer_id: o for o in make_store(PriceRoStore, FakeHttp({"price.ro/rezultate~": html})).search("s25")}
    assert by["5300001"].price == 6499.99 and by["5300001"].shop_count == 9
    assert by["5300002"].price == 3899.0


class _RedirectHttp(FakeHttp):
    def __init__(self, routes, final):
        super().__init__(routes)
        self.final = final

    def request(self, method, url, **kw):
        r = super().request(method, url, **kw)
        return Response(url=self.final, status=r.status, text=r.text, headers={})


def test_pricero_redirect_to_home_page_returns_nothing():
    store = make_store(PriceRoStore, _RedirectHttp({"price.ro/": HTML}, "https://www.price.ro/"))
    assert store.search("iphone 15 pro") == [] and store.last_strategy == "redirect-home"


def test_pricero_search_landing_on_product_page():
    url = "https://www.price.ro/preturi-apple-iphone-15-pro-1tb-black-titanium-4336877"
    html = ('<html><body><h1>Apple iPhone 15 Pro 1TB Black Titanium</h1><div>de la 9.799 lei · 6 magazine</div>'
            '</body></html>')
    store = make_store(PriceRoStore, _RedirectHttp({"price.ro/rezultate~": html}, url))
    (o,) = store.search("apple iphone 15 pro 1tb black titanium")
    assert store.last_strategy == "product-page" and o.offer_id == "4336877" and o.price == 9799.0


def test_pricero_id():
    assert pricero_id("https://www.price.ro/preturi-apple-iphone-15-pro-1tb-black-titanium-4336877") == "4336877"
    assert pricero_id("https://www.price.ro/preturi-smartphone/") is None


def test_aggregator_text_helpers_keep_model_digits_apart():
    from bot.stores._aggregators import count_in, first_price

    html = ('<html><body><div><div class="p"><a href="/preturi-samsung-galaxy-s24-128gb-5200001">Samsung Galaxy S24</a>'
            ' 999 lei <span>12 magazine</span></div>'
            '<div class="p"><a href="/preturi-lg-oled55c4-5200002">LG OLED55C4</a> de la 4.199 lei</div></div></body></html>')
    offers = make_store(PriceRoStore, FakeHttp({"price.ro/rezultate~": html})).search("tv")
    by = {o.offer_id: o for o in offers}
    assert by["5200001"].price == 999.0 and by["5200001"].shop_count == 12
    assert by["5200002"].price == 4199.0
    assert count_in("Galaxy S24 12 oferte") == 12 and count_in("în 9 magazine") == 9 and count_in("emag.ro") is None
    assert first_price("1 299 – 1 499 lei") == 1299.0 and first_price("de la 1 989,00 RON") == 1989.0
    assert first_price("Momentan indisponibil") is None
