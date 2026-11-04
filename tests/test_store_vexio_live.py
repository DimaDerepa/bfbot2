"""Vexio against a REAL search page (tests/fixtures/vexio_live.html, trimmed capture of
https://www.vexio.ro/search?q=&q=iphone+16+pro from GitHub Actions, 2026-11-04).

The real page holds no product at all: it is a shell (`search_frontend_provider = 'luigisbox'` + the LBX-784749.js
loader) that Luigi's Box fills in the browser. The Luigi's Box loader and API answers below are NOT captures (the
sandbox could not reach them): their shape follows the Luigi's Box search API, the values are made up.
"""
import os

import pytest

from bot.extract import make_soup
from bot.stores import vexio as vexio_mod
from bot.stores.vexio import VexioStore, find_tracker_id
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
LIVE = open(os.path.join(FIX, "vexio_live.html"), encoding="utf-8").read()

SEARCH = "https://www.vexio.ro/search/?q=iphone+16+pro"
LBX_TECH = "https://scripts.luigisbox.tech/LBX-784749.js"
LBX_COM = "https://scripts.luigisbox.com/LBX-784749.js"
API = "https://live.luigisbox.com/search?tracker_id=784749-123456&q=iphone+16+pro&f%5B%5D=type%3Aitem&size=24&page=1"
LBX_JS = ('!function(w){w.Luigis=w.Luigis||{};w.Luigis.config={"TrackerId":"784749-123456","Locale":"ro",'
          '"Types":[{"type":"item","size":24},{"type":"category","size":5}]}}(window);')
HITS = {"results": {"total_hits": 5, "hits": [
    {"url": "https://www.vexio.ro/smartphone/apple/190001-iphone-16-pro-128gb-black-titanium/?utm_source=lb#x",
     "type": "item",
     "attributes": {"title": "Telefon mobil Apple iPhone 16 Pro, 128GB, 5G, Black Titanium", "price_amount": 5299.99,
                    "price_old_amount": 5999.99, "availability": 1, "id": ["190001"],
                    "image_link": "/images/products/190001/thumb.jpg"}},
    {"url": "/smartphone/apple/190002-iphone-16-pro-256gb-desert-titanium/", "type": "item",
     "attributes": {"title": ["Telefon mobil Apple iPhone 16 Pro, 256GB, 5G, <em>Desert</em> Titanium"],
                    "price": "6.149,90 lei", "price_old": "6.149,90 lei", "availability": "0",
                    "availability_rank_text": "Stoc epuizat"}},
    {"url": "https://www.vexio.ro/huse-telefoane/apple/190003-husa-iphone-16-pro-clear-case-magsafe/", "type": "item",
     "attributes": {"title": "Husa Apple iPhone 16 Pro Clear Case cu MagSafe", "price_amount": "249.99",
                    "availability_text": "In stoc"}},
    {"url": "https://www.vexio.ro/smartphone/", "type": "category", "attributes": {"title": "Smartphone"}},
    {"url": "https://www.vexio.ro/smartphone/apple/190004-iphone-16-pro-max-1tb/", "type": "item",
     "attributes": {"title": "Telefon mobil Apple iPhone 16 Pro Max, 1TB (pret la cerere)"}},
    {"url": "https://www.other-shop.ro/p/190005/", "type": "item",
     "attributes": {"title": "iPhone 16 Pro", "price_amount": 4999.0}},
]}}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv(vexio_mod.TRACKER_ENV, raising=False)

    def reset():
        VexioStore._route, VexioStore._probed = None, False
        VexioStore._tracker, VexioStore._tracker_at, VexioStore._lb_type = None, 0.0, None
    reset()
    yield
    reset()


def test_live_page_is_a_luigisbox_shell_without_products():
    assert VexioStore._is_search_page(make_soup(LIVE), "iphone 16 pro")
    assert "search_frontend_provider = 'luigisbox'" in LIVE and LBX_TECH in LIVE
    assert find_tracker_id(LIVE) is None                                 # the id is only in the loader
    store = make_store(VexioStore, FakeHttp({}))
    assert store._parse(make_soup(LIVE), SEARCH) == []         # mega-menu / footer links are no offers


def test_live_search_goes_through_luigisbox():
    http = FakeHttp({"vexio.ro/search/?q=": LIVE, "LBX-784749.js": LBX_JS, "live.luigisbox.com/search": HITS})
    store = make_store(VexioStore, http)
    offers = store.search("iphone 16 pro")
    assert http.calls == [SEARCH, LBX_TECH, API]
    assert store.last_strategy == "luigisbox"
    assert http.headers_sent[2].get("Origin") == "https://www.vexio.ro"
    by = {o.offer_id: o for o in offers}
    assert list(by) == ["190001", "190002", "190003"]       # category, price-less and foreign hits dropped
    a = by["190001"]
    assert a.url == "https://www.vexio.ro/smartphone/apple/190001-iphone-16-pro-128gb-black-titanium/"
    assert a.title == "Telefon mobil Apple iPhone 16 Pro, 128GB, 5G, Black Titanium"
    assert (a.price, a.old_price, a.in_stock, a.currency) == (5299.99, 5999.99, True, "RON")
    assert a.image == "https://www.vexio.ro/images/products/190001/thumb.jpg"
    b = by["190002"]
    assert b.url == "https://www.vexio.ro/smartphone/apple/190002-iphone-16-pro-256gb-desert-titanium/"
    assert b.title == "Telefon mobil Apple iPhone 16 Pro, 256GB, 5G, Desert Titanium"
    assert (b.price, b.old_price, b.in_stock) == (6149.9, None, False)    # old == price is no discount
    c = by["190003"]
    assert (c.price, c.old_price, c.in_stock) == (249.99, None, True)
    assert VexioStore._route == "https://www.vexio.ro/search/?q={q}" and VexioStore._lb_type == "item"
    http.calls.clear()
    make_store(VexioStore, http).search("iphone 16 pro")                  # tracker cached: API only
    assert http.calls == [API]


def test_live_shell_without_reachable_loader_returns_nothing():
    http = FakeHttp({"vexio.ro/search/?q=": LIVE})
    store = make_store(VexioStore, http)
    assert store.search("iphone 16 pro") == []
    assert store.last_strategy == "luigisbox:no-tracker-id"
    assert http.calls == [SEARCH, LBX_TECH, LBX_COM]
    http.calls.clear()
    assert make_store(VexioStore, http).search("iphone 16 pro") == []
    assert http.calls == [SEARCH]                                         # loader not re-fetched within the hour


def test_env_tracker_and_index_type_fallback(monkeypatch):
    monkeypatch.setenv(vexio_mod.TRACKER_ENV, "11111-22222")
    http = FakeHttp({"type%3Aitem": {"results": {"hits": [], "total_hits": 0}}, "type%3Aproduct": HITS})
    offers = make_store(VexioStore, http).search("iphone 16 pro")
    assert len(offers) == 3 and VexioStore._lb_type == "product"
    assert http.calls == [
        "https://live.luigisbox.com/search?tracker_id=11111-22222&q=iphone+16+pro&f%5B%5D=type%3Aitem&size=24&page=1",
        "https://live.luigisbox.com/search?tracker_id=11111-22222&q=iphone+16+pro&f%5B%5D=type%3Aproduct&size=24&page=1",
    ]


def test_paging_stops_at_total_hits():
    hit = HITS["results"]["hits"][0]
    many = {"results": {"total_hits": 24, "hits": [
        {**hit, "url": f"https://www.vexio.ro/smartphone/apple/{191000 + i}-iphone-16-pro-{i}/",
         "attributes": {**hit["attributes"], "id": [str(191000 + i)]}} for i in range(24)]}}
    VexioStore._tracker = "784749-123456"
    http = FakeHttp({"live.luigisbox.com/search": many})
    assert len(make_store(VexioStore, http, max_pages=3).search("iphone 16 pro")) == 24
    assert len(http.calls) == 1                                           # 24 of 24: page 2 never asked


def test_tracker_id_patterns():
    assert find_tracker_id('{"TrackerId":"784749-123456"}') == "784749-123456"
    assert find_tracker_id("x='784749-998877';", "784749") == "784749-998877"
    assert find_tracker_id("Sau suna la numarul 0374 477 111. 2026-11-04") is None
