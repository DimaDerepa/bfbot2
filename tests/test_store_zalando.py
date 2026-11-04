import os

import pytest

from bot.net import BlockedError
from bot.stores.zalando import ZalandoStore, graphql_cache, zalando_sku
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HTML = open(os.path.join(FIX, "zalando_search.html"), encoding="utf-8").read()
PDP = open(os.path.join(FIX, "zalando_product.html"), encoding="utf-8").read()


def test_zalando_graphql_cache_and_grid():
    http = FakeHttp({"zalando.ro/catalog/": HTML})
    store = make_store(ZalandoStore, http)
    offers = store.search("adidas samba")
    assert http.calls == ["https://www.zalando.ro/catalog/?q=adidas+samba"]
    assert store.last_strategy == "graphql+grid"
    assert [o.offer_id for o in offers] == ["AD115O0NI-A11", "NI112O0GU-A12", "LE222N0AB-K11"]   # slot order
    a, b, c = offers
    assert a.title == "adidas Originals SAMBA OG - Sneakers low - white/black"
    assert a.url == "https://www.zalando.ro/adidas-originals-samba-og-sneakers-low-white-ad115o0ni-a11.html"
    assert a.price == 449.99                      # promotional amount in bani
    assert a.old_price == 499.99                  # EU Omnibus 30-day low wins over the struck 549,99
    assert a.image.startswith("https://img01.ztat.net/")
    assert b.title == "Nike Sportswear AIR MAX 90 - Sneakers low"          # leading "- " dropped
    assert b.price == 1299.99 and b.old_price is None
    # third card exists only in the rendered grid: "1.149,00 lei" / "1.349,00 lei" / "1.199,00 lei"
    assert c.title == "Levi's® 501® ORIGINAL - Jeans drepti - stonewash"
    assert c.price == 1149.0 and c.old_price == 1199.0
    assert c.url == "https://www.zalando.ro/levis-501-original-jeans-drepti-le222n0ab-k11.html"
    assert all(o.in_stock is None for o in offers)


def test_zalando_page_urls_and_last_page():
    store = make_store(ZalandoStore, FakeHttp({}))
    assert store.search_url("nike air max 90", 1) == "https://www.zalando.ro/catalog/?q=nike+air+max+90"
    assert store.search_url("nike air max 90", 2) == "https://www.zalando.ro/catalog/?q=nike+air+max+90&p=2"
    assert store.search_url("levi's 501", 1) == "https://www.zalando.ro/catalog/?q=levi%27s+501"
    http = FakeHttp({"zalando.ro/catalog/": HTML})
    s = make_store(ZalandoStore, http)
    s.search_page("adidas samba", 1)
    assert s._pages == 3
    assert s.search_page("adidas samba", 4) == [] and len(http.calls) == 1


def test_zalando_grid_only_page():
    html = HTML.split("<script>runtime")[0] + "</body></html>"
    offers = make_store(ZalandoStore, FakeHttp({"zalando.ro/catalog/": html})).search("adidas samba")
    assert [o.offer_id for o in offers] == ["AD115O0NI-A11", "LE222N0AB-K11"]
    assert offers[0].price == 449.99 and offers[0].old_price == 499.99     # 449,99 / 549,99 / 499,99 lei lines


def test_zalando_product_page():
    url = "https://www.zalando.ro/adidas-originals-samba-og-sneakers-low-white-ad115o0ni-a11.html"
    o = make_store(ZalandoStore, FakeHttp({"zalando.ro/adidas-originals": PDP})).product(url)
    assert o is not None
    assert o.price == 449.99 and o.old_price == 549.99 and o.in_stock is True    # cheapest in-stock size
    assert o.offer_id == "AD115O0NI-A11" and o.url == url
    assert o.title == "adidas Originals SAMBA OG - Sneakers low - white/black"


def test_zalando_sku_search_redirects_to_product():
    http = FakeHttp({"zalando.ro/catalog/": PDP})
    store = make_store(ZalandoStore, http)
    orig = http.request

    def request(method, url, **kw):
        r = orig(method, url, **kw)
        r.url = "https://www.zalando.ro/adidas-originals-samba-og-sneakers-low-white-ad115o0ni-a11.html"
        return r
    http.request = request
    offers = store.search("AD115O0NI-A11")
    assert http.calls == ["https://www.zalando.ro/catalog/?q=AD115O0NI-A11"]
    assert len(offers) == 1 and offers[0].offer_id == "AD115O0NI-A11" and store.last_strategy == "product-page"


def test_zalando_challenge_200():
    page = "<html><head><title>Pardon Our Interruption</title></head><body><p>Please enable JS</p></body></html>"
    with pytest.raises(BlockedError):
        make_store(ZalandoStore, FakeHttp({"zalando.ro": page})).search("x")


def test_zalando_helpers():
    assert zalando_sku("https://www.zalando.ro/x-y-ad115o0ni-a11.html?x=1") == "AD115O0NI-A11"
    assert zalando_sku("https://www.zalando.ro/femei/") is None
    assert graphql_cache("<script>window.__hydrationDataConsume({broken</script>") == {}
