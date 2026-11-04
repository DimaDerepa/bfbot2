import os

import pytest

from bot.net import BlockedError, FetchError
from bot.stores import douglas as douglas_mod
from bot.stores.douglas import DouglasStore, douglas_id
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "douglas_search_shopware.html"), encoding="utf-8").read()

GROUP_HTML = """<html><body><div class="product-grid">
<div data-testid="product-tile" class="product-tile">
  <a class="link product-tile__main-link" href="/ro/p/5010213189?variant=1027645&trac=abc">
    <div class="text top-brand">Yves Saint Laurent</div><div class="text brand-line">Libre</div>
    <div class="text name">Eau de Parfum</div><div class="text category">Apă de parfum</div>
  </a>
  <div class="product-price"><span class="product-price__original">729,00 Lei</span>
    <span class="product-price__price product-price__price--discount">583,20 Lei</span>
    <span class="product-price__base">1.166,40 Lei / 100 ml</span></div>
</div></div></body></html>"""


@pytest.fixture(autouse=True)
def _fresh_route():
    douglas_mod._reset_route_cache()
    yield
    douglas_mod._reset_route_cache()


def test_douglas_shopware_listing():
    http = FakeHttp({"douglas.ro/search?search=": HTML})
    store = make_store(DouglasStore, http)
    offers = store.search("lancome")
    assert http.calls == ["https://www.douglas.ro/search?search=lancome"]
    assert store.last_strategy == "shopware-cards"
    assert len(offers) == 3
    a, b, c = offers
    assert a.title == "Lancôme La Vie Est Belle Apă de parfum"
    assert a.url == "https://www.douglas.ro/lancome-la-vie-est-belle-apa-de-parfum/LAN3605532612690"
    assert a.price == 549.0 and a.old_price == 699.0              # "549,00 Lei*" + list price, unit price ignored
    assert a.offer_id == "0189a1b2c3d44e5f8a9b0c1d2e3f4a5b"
    assert a.image == "https://www.douglas.ro/media/a7/2c/0f/1689333101/3605532612690.jpg"
    assert b.price == 1249.0 and b.old_price is None
    assert b.url == "https://www.douglas.ro/lancome-advanced-genifique-ser/LAN3614273069540"
    assert c.title == "Lancôme Idôle Apă de parfum" and c.price == 389.0 and c.in_stock is False
    assert a.in_stock is None and b.in_stock is None


def test_douglas_probes_second_route_on_404_and_remembers_it():
    http = FakeHttp({"douglas.ro/search?search=": (404, "<html><title>Pagina nu a fost gasita</title></html>"),
                     "douglas.ro/ro/search?q=": GROUP_HTML})
    store = make_store(DouglasStore, http)
    offers = store.search("ysl libre")
    assert http.calls == ["https://www.douglas.ro/search?search=ysl+libre", "https://www.douglas.ro/ro/search?q=ysl+libre"]
    assert store.last_strategy == "douglas-tiles"
    o = offers[0]
    assert o.title == "Yves Saint Laurent Libre Eau de Parfum Apă de parfum"
    assert o.url == "https://www.douglas.ro/ro/p/5010213189?variant=1027645"
    assert o.offer_id == "5010213189-1027645"
    assert o.price == 583.2 and o.old_price == 729.0
    store.search("ysl libre")                                    # the working route is tried first next time
    assert http.calls[2] == "https://www.douglas.ro/ro/search?q=ysl+libre"


def test_douglas_both_routes_missing_raises_fetch_error():
    http = FakeHttp({})
    with pytest.raises(FetchError) as e:
        make_store(DouglasStore, http).search("x")
    assert not isinstance(e.value, BlockedError) and len(http.calls) == 2


def test_douglas_id():
    assert douglas_id("https://www.douglas.ro/ro/p/5010213189") == "5010213189"
    assert douglas_id("https://www.douglas.ro/ro/p/5010213189?variant=1") == "5010213189-1"
    assert douglas_id("https://www.douglas.ro/lancome/LAN1") is None
