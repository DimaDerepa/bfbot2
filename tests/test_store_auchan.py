import json
import os

from bot.stores.auchan import AuchanStore
from tests.conftest import FakeHttp, make_store

API = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "auchan_search.json"), encoding="utf-8"))
IS = "auchan.ro/api/io/_v/api/intelligent-search/product_search/"


def test_auchan_intelligent_search():
    http = FakeHttp({IS: API})
    store = make_store(AuchanStore, http)
    offers = store.search("televizor samsung")
    assert http.calls == ["https://www.auchan.ro/api/io/_v/api/intelligent-search/product_search/"
                          "?query=televizor+samsung&count=50&page=1&locale=ro-RO"]
    assert store.last_strategy == "vtex-intelligent-search"
    by = {o.offer_id: o for o in offers}
    assert set(by) == {"1029384", "5512", "2044001", "2044777"}       # product without sellers skipped
    tv = by["1029384"]
    assert tv.url == "https://www.auchan.ro/televizor-smart-led-samsung-ue55du7172uxxh-138-cm-4k-ultra-hd-clasa-g/p"
    assert tv.price == 1799.9 and tv.old_price == 2299.9 and tv.in_stock is True and tv.seller is None
    assert tv.image.startswith("https://auchan.vtexassets.com/arquivos/ids/1771231/")
    banana = by["5512"]                                              # sold by kg: package = 0.2 kg
    assert banana.price == 1.4 and banana.old_price == 1.6
    sb = by["2044001"]                                               # Auchan seller has no price -> marketplace offer
    assert sb.price == 849.99 and sb.old_price == 999.99 and sb.seller == "Electro Vision SRL" and sb.in_stock is True
    q = by["2044777"]
    assert q.url == "https://www.auchan.ro/televizor-samsung-qe65q60d-163-cm-qled-4k/p"
    assert q.in_stock is False and q.old_price is None


def test_auchan_pages_stop_on_short_page():
    http = FakeHttp({IS: API})
    store = make_store(AuchanStore, http, max_pages=3)
    store.search("tv")
    assert len(http.calls) == 1                                      # 5 products < count=50 -> last page


def test_auchan_legacy_fallback():
    legacy = [
        {"productId": "77", "productName": "Laptop ASUS Vivobook 15", "link": "https://www.auchan.ro/laptop-asus/p?skuId=77",
         "items": [{"images": [{"imageUrl": "https://auchan.vtexassets.com/arquivos/ids/77/asus.jpg"}],
                    "sellers": [{"sellerName": "Auchan Romania",
                                 "commertialOffer": {"Price": 2499.0, "ListPrice": 2999.0, "AvailableQuantity": 2}}]}]},
        # same product objects as Intelligent Search -> same rules: per-kg price x unitMultiplier, marketplace seller
        {"productId": "5512", "productName": "Banane, per kg", "linkText": "banane-per-kg",
         "items": [{"measurementUnit": "kg", "unitMultiplier": 0.2,
                    "sellers": [{"sellerName": "Fresh Market SRL",
                                 "commertialOffer": {"Price": 6.99, "ListPrice": 7.99, "AvailableQuantity": 0}}]}]},
    ]
    http = FakeHttp({IS: (404, "not found"), "/api/catalog_system/pub/products/search": legacy})
    store = make_store(AuchanStore, http)
    offers = store.search("laptop asus")
    assert http.calls[1] == "https://www.auchan.ro/api/catalog_system/pub/products/search?ft=laptop+asus&_from=0&_to=49"
    assert store.last_strategy == "vtex-legacy"
    a, b = offers
    assert a.price == 2499.0 and a.old_price == 2999.0 and a.in_stock is True and a.seller is None
    assert a.url == "https://www.auchan.ro/laptop-asus/p" and a.image.endswith("/asus.jpg")
    assert b.price == 1.4 and b.old_price == 1.6 and b.in_stock is False and b.seller == "Fresh Market SRL"
    assert b.url == "https://www.auchan.ro/banane-per-kg/p"


def test_auchan_unexpected_shape_uses_legacy():
    http = FakeHttp({IS: {"error": "query too long"}, "/api/catalog_system/pub/products/search": []})
    store = make_store(AuchanStore, http)
    assert store.search("x") == [] and store.last_strategy == "vtex-legacy"
    # an empty Intelligent Search answer is a real "no results": no legacy call
    http = FakeHttp({IS: {"products": [], "recordsFiltered": 0}})
    store = make_store(AuchanStore, http, max_pages=3)
    assert store.search("xyzzy") == [] and len(http.calls) == 1


def test_auchan_product_page_api():
    legacy = [API["products"][0]]
    http = FakeHttp({"/api/catalog_system/pub/products/search/": legacy})
    url = "https://www.auchan.ro/televizor-smart-led-samsung-ue55du7172uxxh-138-cm-4k-ultra-hd-clasa-g/p"
    o = make_store(AuchanStore, http).product(url)
    assert http.calls == ["https://www.auchan.ro/api/catalog_system/pub/products/search/"
                          "televizor-smart-led-samsung-ue55du7172uxxh-138-cm-4k-ultra-hd-clasa-g/p"]
    assert o.price == 1799.9 and o.old_price == 2299.9 and o.url == url and o.store_name == "Auchan"
