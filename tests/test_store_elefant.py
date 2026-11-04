import os

import pytest

from bot.net import BlockedError
from bot.stores.elefant import ElefantStore, elefant_id
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "elefant_search.html"), encoding="utf-8").read()


def test_elefant_cards():
    http = FakeHttp({"elefant.ro/search": HTML})
    store = make_store(ElefantStore, http)
    offers = store.search("harry potter")
    assert http.calls == ["https://www.elefant.ro/search?SearchTerm=harry%20potter"]
    assert store.last_strategy == "cards"
    assert len(offers) == 3                                   # "Pret indisponibil" card has no price
    book, lego, perfume = offers
    assert book.title == "Arthur Harry Potter si piatra filozofala - editie ilustrata"
    assert book.url == "https://www.elefant.ro/harry-potter-si-piatra-filozofala-editie-ilustrata_5f2e8c1d.html"
    assert book.price == 89.99 and book.old_price == 129.99 and book.in_stock is True
    assert book.offer_id == "9786063312478"                   # data-product-id wins over the URL hash
    assert book.image == "https://cdn.elefant.ro/images/78/2534278/harry-potter-1_1_size3.jpg"
    assert lego.price == 2299.0 and lego.old_price == 2899.99 and lego.in_stock is True
    assert lego.offer_id == "235896"
    assert perfume.price == 119.9 and perfume.old_price is None and perfume.in_stock is False
    assert perfume.offer_id == "a1b2c3d4e5"


def test_elefant_next_page_from_pager():
    page2 = HTML.replace('data-product-id="9786063312478"', 'data-product-id="X2000"').replace('rel="next"', 'rel="prev"') \
        .replace('aria-label="Urmatoarea"', 'aria-label="Inapoi"')
    http = FakeHttp({"PageNumber=1": page2, "elefant.ro/search?SearchTerm=": HTML})
    store = make_store(ElefantStore, http, max_pages=3)
    assert len(store.search_page("harry potter", 1)) == 3
    p2 = store.search_page("harry potter", 2)              # follows the pager link, never a built URL
    assert http.calls == ["https://www.elefant.ro/search?SearchTerm=harry%20potter",
                          "https://www.elefant.ro/search?SearchTerm=harry%20potter&PageNumber=1"]
    assert p2[0].offer_id == "X2000"
    assert store.search_page("harry potter", 3) == [] and len(http.calls) == 2   # no next link on page 2


def test_elefant_query_encoding():
    store = make_store(ElefantStore, FakeHttp({}))
    assert store.search_url("crema de fata spf 50+") == "https://www.elefant.ro/search?SearchTerm=crema%20de%20fata%20spf%2050%2B"


def test_elefant_cloudflare():
    with pytest.raises(BlockedError):
        make_store(ElefantStore, FakeHttp({"elefant.ro": (403, "<title>Just a moment...</title>")})).search("x")


def test_elefant_id():
    assert elefant_id("https://www.elefant.ro/x/carte-12345.html") == "12345"
    assert elefant_id("https://www.elefant.ro/x/carte_ab12cd34.html") == "ab12cd34"
    assert elefant_id("https://www.elefant.ro/carti/") is None


ELEFANT_PDP = """<html><head><title>Harry Potter si piatra filozofala | elefant.ro</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Harry Potter si piatra filozofala",
 "sku":"9786063312478","offers":{"@type":"Offer","price":"1.299,00","priceCurrency":"RON",
 "availability":"https://schema.org/InStock"}}</script></head><body><h1>Harry Potter si piatra filozofala</h1>
<div class="related"><div class="product-tile"><a class="product-title" href="/harry-potter-2-12345.html">Harry Potter 2</a>
<div class="product-price"><div class="current-price">59,99 lei</div></div></div></div></body></html>"""


def test_elefant_search_redirected_to_product_page():
    http = FakeHttp({"elefant.ro/search": ELEFANT_PDP})
    orig = http.request

    def request(method, url, **kw):
        r = orig(method, url, **kw)
        r.url = "https://www.elefant.ro/harry-potter-si-piatra-filozofala_5f2e8c1d.html"
        return r
    http.request = request
    store = make_store(ElefantStore, http)
    offers = store.search("harry potter piatra filozofala")
    assert store.last_strategy == "product-page"
    assert [(o.title, o.price, o.in_stock) for o in offers] == [("Harry Potter si piatra filozofala", 1299.0, True)]
    assert offers[0].url == "https://www.elefant.ro/harry-potter-si-piatra-filozofala_5f2e8c1d.html"
