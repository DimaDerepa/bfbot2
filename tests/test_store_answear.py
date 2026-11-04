import json
import os

import pytest

from bot.stores import answear as answear_mod
from bot.stores.answear import AnswearStore, answear_id, find_tracker_id
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HOME = open(os.path.join(FIX, "answear_home.html"), encoding="utf-8").read()
HITS = json.load(open(os.path.join(FIX, "answear_luigis.json"), encoding="utf-8"))


@pytest.fixture(autouse=True)
def _fresh_tracker(monkeypatch):
    answear_mod._reset_tracker_cache()
    monkeypatch.delenv(answear_mod.TRACKER_ENV, raising=False)
    yield
    answear_mod._reset_tracker_cache()


def test_answear_luigisbox_search():
    http = FakeHttp({"live.luigisbox.com/search": HITS, "https://answear.ro/": HOME})
    store = make_store(AnswearStore, http)
    offers = store.search("tommy jeans")
    assert http.calls == [
        "https://answear.ro/",                                            # tracker id discovery (once)
        "https://live.luigisbox.com/search?tracker_id=312904-418722&q=tommy+jeans&f%5B%5D=type%3Aproduct"
        "&size=24&page=1",
    ]
    assert store.last_strategy == "luigisbox"
    by = {o.offer_id: o for o in offers}
    assert list(by) == ["1209285", "1188460", "1210001"]          # category hit and price-less hit dropped
    t = by["1209285"]
    assert t.title == "Tommy Jeans tricou din bumbac culoarea albastru marin, neted"   # brand not repeated
    assert t.url == "https://answear.ro/p/tommy-jeans-tricou-din-bumbac-culoarea-albastru-marin-neted-1209285.html"
    assert t.price == 119.9 and t.old_price == 179.9 and t.in_stock is True
    assert t.image == "https://img2.ans-media.com/i/356x473/AW26-TSM0Z7_59X_F1.jpg"
    g = by["1188460"]
    assert g.title == "Tommy Jeans geaca de puf barbati, culoarea negru, de iarna"
    assert g.url == "https://answear.ro/p/tommy-jeans-geaca-de-puf-barbati-culoarea-negru-de-iarna-1188460.html"
    assert g.price == 1299.0 and g.old_price is None and g.in_stock is False
    s = by["1210001"]
    assert s.price == 1249.0 and s.in_stock is None and s.title == "Tommy Jeans Sneakers din piele"
    # second search in the same process: tracker id is cached
    store.search("tommy jeans")
    assert http.calls.count("https://answear.ro/") == 1


def test_answear_tracker_from_lbx_script_and_env(monkeypatch):
    home = HOME.replace('"trackerId":"312904-418722",', "")
    js = 'window.Luigis=window.Luigis||{};Luigis.Scan({"TrackerId":"312904-777001","Locale":"ro"});'
    http = FakeHttp({"live.luigisbox.com/search": HITS, "scripts.luigisbox.com/LBX-312904.js": js, "https://answear.ro/": home})
    make_store(AnswearStore, http).search("tricou")
    assert http.calls[1] == "https://scripts.luigisbox.com/LBX-312904.js"
    assert "tracker_id=312904-777001&q=tricou&" in http.calls[2]
    answear_mod._reset_tracker_cache()
    monkeypatch.setenv(answear_mod.TRACKER_ENV, "11111-22222")
    http2 = FakeHttp({"live.luigisbox.com/search": HITS})
    make_store(AnswearStore, http2).search("tricou")
    assert http2.calls[0].startswith("https://live.luigisbox.com/search?tracker_id=11111-22222&q=tricou&")


def test_answear_without_tracker_returns_nothing():
    http = FakeHttp({"https://answear.ro/": "<html><body>no search config</body></html>"})
    store = make_store(AnswearStore, http)
    assert store.search("rochie") == [] and store.last_strategy == "luigisbox:no-tracker-id"
    assert http.calls == ["https://answear.ro/"]


def test_answear_helpers():
    assert find_tracker_id('{"tracker_id": "123456-654321"}') == "123456-654321"
    assert find_tracker_id("2026-11-04 0371-123456") is None
    assert answear_id("https://answear.ro/p/x-y-1209285.html") == "1209285"


def test_answear_text_prices_with_thousands_and_absolute_url(monkeypatch):
    monkeypatch.setenv(answear_mod.TRACKER_ENV, "312904-418722")
    data = {"results": {"total_hits": 1, "hits": [{
        "type": "product", "url": "https://answear.ro/p/the-north-face-geaca-de-puf-1300777.html?lb=1#x",
        "attributes": {"title": ["Geaca de puf Nuptse"], "brand": ["The North Face"], "price": ["1.299,00 lei"],
                       "price_old": "1.599,00 lei", "availability": "1", "id": ["1300777"],
                       "image_link": "https://img2.ans-media.com/i/356x473/AW26-KUD0EA_99X_F1.jpg"}}]}}
    offers = make_store(AnswearStore, FakeHttp({"live.luigisbox.com/search": data})).search("nuptse")
    assert len(offers) == 1
    o = offers[0]
    assert o.title == "The North Face Geaca de puf Nuptse"
    assert o.price == 1299.0 and o.old_price == 1599.0 and o.in_stock is True and o.offer_id == "1300777"
    assert o.url == "https://answear.ro/p/the-north-face-geaca-de-puf-1300777.html"


def test_answear_tracker_miss_is_retried_after_ttl(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(answear_mod.time, "monotonic", lambda: clock[0])
    http = FakeHttp({"https://answear.ro/": "<html>maintenance</html>"})
    store = make_store(AnswearStore, http)
    assert store.search("rochie") == [] and store.search("rochie") == []
    assert http.calls == ["https://answear.ro/"]                       # miss cached for a while
    clock[0] += answear_mod._MISS_TTL + 1                              # daemon mode: an hour later it is retried
    http.routes = {"live.luigisbox.com/search": HITS, "https://answear.ro/": HOME}
    assert store.search("tommy jeans") and http.calls[1] == "https://answear.ro/"
