import os

import pytest

from bot.net import BlockedError
from bot.stores.emag import EmagStore, _listing_items
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "emag_search.html"), encoding="utf-8").read()


def test_emag_cards_and_listing_json():
    http = FakeHttp({"emag.ro/search/": HTML, "https://www.emag.ro/": "<html>home</html>"})
    store = make_store(EmagStore, http)
    offers = store.search("iphone 16 pro")
    assert http.calls[0] == "https://www.emag.ro/"                      # warm-up visit
    assert http.calls[1] == "https://www.emag.ro/search/iphone%2016%20pro"
    by = {o.offer_id: o for o in offers}
    assert set(by) == {"DTN6C3YBM", "D1234ABBM", "DRES16PBM"}            # rec card skipped
    a = by["DTN6C3YBM"]
    assert a.price == 5299.99
    assert a.old_price == 5899.99                                       # JSON lowest_price_30_days wins over card text
    assert a.in_stock is True and a.seller is None
    assert a.url == "https://www.emag.ro/telefon-mobil-apple-iphone-16-pro-256gb-5g-desert-titanium-myny3zd-a/pd/DTN6C3YBM/"
    assert by["D1234ABBM"].price == 49.99                                # <sup> without comma
    r = by["DRES16PBM"]
    assert r.price == 4149.99 and r.in_stock is True and r.seller == "Mobile Store SRL"
    assert store.last_strategy == "cards+json"


def test_emag_listing_json_only():
    html = '<html><script>EM.listingGlobals.items = [{"id":1,"name":"Consola Sony PlayStation 5 Slim","url":{"path":"/ps5/pd/DPS5SLBM/"},"offer":{"price":{"current":2199.0,"lowest_price_30_days":{"amount":2499,"is_visible":true}},"availability":{"text":"Stoc epuizat"}}}];</script></html>'
    offers = make_store(EmagStore, FakeHttp({"emag.ro/search/": html, "https://www.emag.ro/": "ok"})).search("ps5")
    assert len(offers) == 1 and offers[0].old_price == 2499 and offers[0].in_stock is False


def test_emag_listing_items_parser():
    assert _listing_items("EM.listingGlobals.items = [];") == []
    assert _listing_items("nothing") is None


def test_emag_blocked():
    http = FakeHttp({"emag.ro": (403, "<html><title>Access denied</title></html>")})
    with pytest.raises(BlockedError):
        make_store(EmagStore, http).search("tv")


def test_emag_query_encoding():
    http = FakeHttp({"emag.ro/search/": "<html>EM.listingGlobals.items = [];</html>", "https://www.emag.ro/": "ok"})
    make_store(EmagStore, http).search("MG6J4ZD/A ssd+hdd")
    assert http.calls[1] == "https://www.emag.ro/search/MG6J4ZD%2FA%20ssd%2Bhdd"
