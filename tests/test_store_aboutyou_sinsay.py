"""ABOUT YOU and Sinsay / Reserved: disabled by default (no verified search route), parsers still tested."""
import json
import os

import pytest

from bot.stores import aboutyou as aboutyou_mod
from bot.stores.aboutyou import AboutYouStore, find_shop_id
from bot.stores.reserved import ReservedStore
from bot.stores.sinsay import SinsayStore, algolia_config
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
AY = json.load(open(os.path.join(FIX, "aboutyou_products.json"), encoding="utf-8"))
SIN_HTML = open(os.path.join(FIX, "sinsay_search.html"), encoding="utf-8").read()
SIN_ALGOLIA = json.load(open(os.path.join(FIX, "sinsay_algolia.json"), encoding="utf-8"))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    aboutyou_mod._reset_shop_cache()
    monkeypatch.delenv(aboutyou_mod.SHOP_ENV, raising=False)
    yield
    aboutyou_mod._reset_shop_cache()


def test_disabled_by_default():
    assert not AboutYouStore.enabled_by_default and not SinsayStore.enabled_by_default
    assert not ReservedStore.enabled_by_default


def test_aboutyou_bapi():
    home = '<html><script>window.__CONFIG__={"shopId": 562, "country": "RO"}</script></html>'
    http = FakeHttp({"api-cloud.aboutyou.de/v1/products": AY, "https://www.aboutyou.ro/": home})
    store = make_store(AboutYouStore, http)
    offers = store.search("geaca jack jones")
    assert http.calls == [
        "https://www.aboutyou.ro/",
        "https://api-cloud.aboutyou.de/v1/products?with=attributes:key(brand|name),priceRange,variants,images"
        "&filters%5Bterm%5D=geaca%20jack%20jones&sortScore=category_scores&sortChannel=web_default&page=1&perPage=40"
        "&shopId=562",
    ]
    assert store.last_strategy == "bapi"
    assert [o.offer_id for o in offers] == ["4363435", "5120044"]           # EUR and empty entities dropped
    g, n = offers
    assert g.title == "JACK & JONES Geacă de primăvară-toamnă 'Rocky'"
    assert g.url == "https://www.aboutyou.ro/p/jack-jones/geaca-de-primavara-toamna-rocky-4363435"
    assert g.price == 229.9 and g.old_price == 328.5 and g.in_stock is True     # 22990 + 9860 bani
    assert g.image == "https://cdn.aboutstatic.com/file/images/9b1a2c3d4e5f60718293a4b5c6d7e8f9.jpg"
    assert n.price == 1299.99 and n.old_price is None and n.in_stock is False
    assert n.image == "https://cdn.aboutstatic.com/file/images/1f2e3d4c5b6a.png"


def test_aboutyou_without_shop_id(monkeypatch):
    http = FakeHttp({"https://www.aboutyou.ro/": "<html>no id</html>"})
    store = make_store(AboutYouStore, http)
    assert store.search("x") == [] and store.last_strategy == "bapi:no-shop-id"
    assert find_shop_id('fetch("/x?shopId=139")') == "139"


def test_sinsay_algolia_from_page_config():
    http = FakeHttp({"algolia.net/1/indexes": SIN_ALGOLIA, "sinsay.com/ro/ro/": SIN_HTML})
    store = make_store(SinsayStore, http)
    offers = store.search("rochie midi")
    assert http.calls == ["https://www.sinsay.com/ro/ro/?query=rochie%20midi",
                          "https://qx7rt9zk2b-dsn.algolia.net/1/indexes/*/queries"]
    assert store.last_strategy == "algolia"
    assert len(offers) == 2
    r, p = offers
    assert r.title == "Rochie midi cu mânecă lungă" and r.offer_id == "9876A-99X"
    assert r.url == "https://www.sinsay.com/ro/ro/rochie-midi-cu-maneca-lunga-9876a-99x"
    assert r.price == 79.99 and r.old_price == 99.99 and r.in_stock is True
    assert p.url == "https://www.sinsay.com/ro/ro/pulover-tricotat-1234b-10x" and p.price == 59.99
    assert algolia_config(SIN_HTML) == ("QX7RT9ZK2B", "0c5f2a7b9d3e4f61a8b2c7d9e0f1a2b3", "lpp_sinsay_ro_ro_products")


def test_sinsay_without_algolia_uses_generic_extraction():
    html = """<html><body><script type="application/ld+json">{"@type":"ItemList","itemListElement":[
      {"@type":"ListItem","item":{"@type":"Product","name":"Tricou basic","url":"https://www.reserved.com/ro/ro/tricou-basic-1a",
       "offers":{"@type":"Offer","price":"29.99","priceCurrency":"RON"}}},
      {"@type":"ListItem","item":{"@type":"Product","name":"Blugi mom","url":"https://www.reserved.com/ro/ro/blugi-mom-2b",
       "offers":{"@type":"Offer","price":"119.99","priceCurrency":"RON"}}}]}</script></body></html>"""
    offers = make_store(ReservedStore, FakeHttp({"reserved.com/ro/ro/": html})).search("tricou")
    assert [o.price for o in offers] == [29.99, 119.99]
