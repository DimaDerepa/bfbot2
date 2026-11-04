import os

import pytest

from bot.net import BlockedError
from bot.stores.carrefour import CarrefourStore
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "carrefour_search.html"), encoding="utf-8").read()


def test_carrefour_search_url_and_parsing():
    http = FakeHttp({"carrefour.ro/catalogsearch/result/": HTML})
    store = make_store(CarrefourStore, http)
    offers = store.search("televizor samsung")
    assert http.calls == ["https://carrefour.ro/catalogsearch/result/?q=televizor+samsung"]
    by = {o.offer_id: o for o in offers}
    assert set(by) == {"19-44502726", "19-44502818", "19-44502031"}
    a = by["19-44502726"]
    assert a.url == "https://carrefour.ro/produse/televizor-smart-led-samsung-43du8572-108-cm-ultra-hd-4k-negru-19-44502726"
    assert a.price == 1599.99 and a.old_price == 1899.99 and a.in_stock is True
    assert a.seller is None                                    # sold by Carrefour itself
    b = by["19-44502818"]                                      # text only: "2.599 99 Lei" = 2599.99
    assert b.price == 2599.99 and b.old_price == 2999.99
    assert b.seller == "Mega Electronic SRL"
    c = by["19-44502031"]
    assert c.url == "https://carrefour.ro/produse/19-44502031"
    assert c.price == 49.99 and c.in_stock is False and c.seller is None   # "Supermarket Online" = own


def test_carrefour_pagination_and_block():
    http = FakeHttp({"carrefour.ro": HTML})
    make_store(CarrefourStore, http).search_page("laptop asus", 3)
    assert http.calls == ["https://carrefour.ro/catalogsearch/result/?q=laptop+asus&p=3"]
    with pytest.raises(BlockedError):
        make_store(CarrefourStore, FakeHttp({"carrefour.ro": (403, "<html><title>Access Denied</title></html>")})).search("tv")
