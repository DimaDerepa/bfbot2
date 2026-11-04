import json
import os

from bot.stores.ikea import IkeaStore
from tests.conftest import FakeHttp, make_store

API = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "ikea_search.json"), encoding="utf-8"))
SIK = "sik.search.blue.cdtapps.com/ro/ro/search-result-page"


def test_ikea_search_request():
    http = FakeHttp({SIK: API})
    store = make_store(IkeaStore, http)
    store.search("billy alb")
    assert http.calls == ["https://sik.search.blue.cdtapps.com/ro/ro/search-result-page?q=billy+alb&size=24&types=PRODUCT"
                          "&c=sr&v=20210322&autocorrect=true&subcategories-style=tree-navigation"]
    h = http.headers_sent[0]
    assert h["Origin"] == "https://www.ikea.com" and h["Referer"] == "https://www.ikea.com/"


def test_ikea_parsing():
    store = make_store(IkeaStore, FakeHttp({SIK: API}))
    offers = store.search("billy")
    assert store.last_strategy == "sik-search"
    by = {o.offer_id: o for o in offers}
    assert set(by) == {"00263850", "59182201", "40477340"}          # content tile + priceless product skipped
    b = by["00263850"]
    assert b.title == "BILLY Bibliotecă, alb, 80x28x202 cm"
    assert b.url == "https://www.ikea.com/ro/ro/p/billy-biblioteca-alb-00263850/"
    assert b.price == 199 and b.old_price is None and b.in_stock is None
    assert b.image.endswith("billy-biblioteca-alb__0625599_pe692385_s5.jpg")
    s = by["59182201"]                                              # "1.499" + "90" previous price, ?ref dropped
    assert s.price == 1299.9 and s.old_price == 1499.9
    assert s.url == "https://www.ikea.com/ro/ro/p/billy-oxberg-biblioteca-cu-usi-alb-s59182201/"
    assert by["40477340"].old_price == 89


def test_ikea_product_by_item_number():
    http = FakeHttp({SIK: API})
    url = "https://www.ikea.com/ro/ro/p/billy-oxberg-biblioteca-cu-usi-alb-s59182201/"
    o = make_store(IkeaStore, http).product(url)
    assert "q=59182201" in http.calls[0]
    assert o.price == 1299.9 and o.old_price == 1499.9 and o.url == url and o.store_name == "IKEA"


def test_ikea_product_short_url_and_other_country():
    http = FakeHttp({SIK: API})
    o = make_store(IkeaStore, http).product("https://www.ikea.com/ro/ro/p/00263850/")   # redirect form, no slug
    assert "q=00263850" in http.calls[0] and o.price == 199 and o.offer_id == "00263850"
    # an ikea.com/de/... page is priced in EUR: never answered from the RO index or read as lei
    http = FakeHttp({SIK: API, "www.ikea.com/de/": '<html><meta property="product:price:amount" content="79.99"></html>'})
    assert make_store(IkeaStore, http).product("https://www.ikea.com/de/de/p/billy-buecherregal-weiss-00263850/") is None
    assert http.calls == []


def test_ikea_product_pip_fallback():
    pip = ('<html><body><h1>BILLY Bibliotecă</h1><div class="pip-price-module__current-price">'
           '<span class="pip-price__integer">1.299</span><span class="pip-price__decimal">,90</span>'
           '<span class="pip-price__currency">Lei</span></div></body></html>')
    http = FakeHttp({SIK: {"searchResultPage": {"products": {"main": {"items": []}}}}, "www.ikea.com/ro/ro/p/": pip})
    o = make_store(IkeaStore, http).product("https://www.ikea.com/ro/ro/p/billy-biblioteca-alb-00263850/")
    assert o.price == 1299.9 and o.title == "BILLY Bibliotecă" and o.offer_id == "00263850"
