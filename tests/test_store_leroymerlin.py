import os

import pytest

from bot.net import BlockedError
from bot.stores.leroymerlin import LeroyMerlinStore, product_id
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "leroymerlin_search.html"), encoding="utf-8").read()


def test_leroymerlin_search_url_path_encoded():
    http = FakeHttp({"leroymerlin.ro/produse/search/": HTML})
    store = make_store(LeroyMerlinStore, http)
    store.search("masina de gaurit")
    assert http.calls == ["https://www.leroymerlin.ro/produse/search/masina%20de%20gaurit"]
    store.search_page("vopsea/lavabila+alb", 2)
    assert http.calls[-1] == "https://www.leroymerlin.ro/produse/search/vopsea%2Flavabila%2Balb?page=2"


def test_leroymerlin_cards():
    store = make_store(LeroyMerlinStore, FakeHttp({"leroymerlin.ro": HTML}))
    offers = store.search("masina de gaurit")
    assert store.last_strategy == "anchors"
    by = {o.offer_id: o for o in offers}
    # card without price skipped, filters ignored
    assert set(by) == {"57358", "11518612", "40021", "61177", "70231", "80412", "70240"}
    b = by["57358"]
    assert b.title == "Mașină de găurit pe acumulator Bosch Professional GSR 18V-55, 18 V, 2 Ah, 2 acumulatori"
    assert b.url == ("https://www.leroymerlin.ro/produse/masini-de-gaurit/1038/"
                     "masina-de-gaurit-pe-acumulator-bosch-professional-18-v-2-ah-2-acumulatori/57358")
    assert b.price == 829.0 and b.old_price == 999.0 and b.in_stock is None and b.seller is None
    assert b.image == "https://media.leroymerlin.ro/media/catalog/product/5/7/57358_1.jpg"
    m = by["11518612"]                                              # marketplace offer
    assert m.title == "Masina de gaurit de banc Bosch PBD40, cu fir, 2500 rpm, autostrangere"
    assert m.price == 2187.90 and m.old_price is None and m.seller == "Scule Pro Expert SRL" and m.in_stock is True
    assert m.image == "https://media.leroymerlin.ro/mirakl/11518612.jpg"
    v = by["40021"]                                                 # per-litre unit price ignored
    assert v.price == 187.90 and v.in_stock is False
    mk = by["61177"]                                                # "-15%" badge, no <del>: min/max; rate ignored
    assert mk.price == 1019.15 and mk.old_price == 1199.0
    # same product linked as "/produse/.../70231" and "https://leroymerlin.ro/produse/.../70231/": still one card
    s = by["70231"]
    assert s.price == 59.9 and s.in_stock is True and s.title == "Set burghie beton Bosch, 5 buc, pentru masina de gaurit"
    assert s.url == "https://www.leroymerlin.ro/produse/accesorii-gaurire/1105/set-burghie-beton-bosch-5-buc/70231"
    assert by["80412"].price == 1049.9                              # only a per-mp price -> used as the price
    assert by["70240"].price == 49.9          # title "...economisire" right before the price is not a savings label
    assert len(offers) == len(by)                                   # no duplicates


def test_leroymerlin_url_canonical_host():
    page = ("<html><body><section><article><a href='https://leroymerlin.ro/produse/a/1038/b/57358/?utm_source=x'>"
            "<h3>Masina de gaurit Bosch GSR 18V</h3></a><span>829,00 lei</span></article>"
            "<article><a href='https://leroymerlin.ro/produse/c-11518612.html?offer=2'><h3>Masina de gaurit de banc PBD40</h3>"
            "</a><span>2.187,90 lei</span></article></section></body></html>")
    offers = make_store(LeroyMerlinStore, FakeHttp({"leroymerlin.ro": page})).search("x")
    assert [o.url for o in offers] == ["https://www.leroymerlin.ro/produse/a/1038/b/57358",
                                       "https://www.leroymerlin.ro/produse/c-11518612.html"]
    assert [o.price for o in offers] == [829.0, 2187.9]


def test_leroymerlin_product_id():
    assert product_id("https://www.leroymerlin.ro/produse/masini-de-gaurit/1038/masina-x/57358") == "57358"
    assert product_id("https://www.leroymerlin.ro/produse/masina-de-banc-11518612.html") == "11518612"
    assert product_id("https://www.leroymerlin.ro/produse/masini-de-gaurit/1038") is None
    assert product_id("https://www.example.ro/produse/a/1/b/2") is None


def test_leroymerlin_datadome_and_empty():
    dd = ("<html><head><title>leroymerlin.ro</title></head><body><script>var dd={'host':'geo.captcha-delivery.com'}"
          "</script></body></html>")
    with pytest.raises(BlockedError):
        make_store(LeroyMerlinStore, FakeHttp({"leroymerlin.ro": (403, dd)})).search("flex")
    empty = "<html><head><title>0 rezultate</title></head><body><h1>0 rezultate pentru: xyzzy</h1></body></html>"
    store = make_store(LeroyMerlinStore, FakeHttp({"leroymerlin.ro": empty}))
    assert store.search("xyzzy") == [] and store.last_strategy == "no-results"
    # "0 rezultate pentru" page that still shows recommended products: they are not results
    rec = ("<html><body><h1>0 rezultate pentru: xyzzy</h1><section><h2>Produse recomandate</h2>"
           "<article><a href='/produse/a/1038/b/57358'><h3>Masina de gaurit Bosch GSR 18V</h3></a><span>829,00 lei</span></article>"
           "<article><a href='/produse/a/1038/c/61177'><h3>Masina de gaurit Makita HP1631K</h3></a><span>399,00 lei</span></article>"
           "</section></body></html>")
    store = make_store(LeroyMerlinStore, FakeHttp({"leroymerlin.ro": rec}))
    assert store.search("xyzzy") == [] and store.last_strategy == "no-results"


def test_leroymerlin_jsonld_itemlist_preferred():
    ld = ('<html><head><title>Cauti flex?</title><script type="application/ld+json">{"@context":"https://schema.org",'
          '"@type":"ItemList","itemListElement":[{"@type":"ListItem","position":1,"item":{"@type":"Product",'
          '"name":"Polizor unghiular Bosch GWS 880, 880 W","url":"/produse/polizoare/1044/polizor-unghiular-bosch-gws-880/61001?pos=1",'
          '"offers":{"@type":"Offer","price":"329.00","priceCurrency":"RON","availability":"https://schema.org/InStock"}}},'
          '{"@type":"ListItem","position":2,"item":{"@type":"Product","name":"Polizor unghiular Makita GA5030R, 720 W",'
          '"url":"https://www.leroymerlin.ro/produse/polizor-makita-ga5030r-11600002.html",'
          '"offers":{"@type":"Offer","price":"289.90","priceCurrency":"RON"}}}]}</script></head><body></body></html>')
    store = make_store(LeroyMerlinStore, FakeHttp({"leroymerlin.ro": ld}))
    offers = store.search("flex")
    assert store.last_strategy == "jsonld"
    assert [(o.offer_id, o.price, o.in_stock) for o in offers] == [("61001", 329.0, True), ("11600002", 289.9, None)]
    assert offers[0].url == "https://www.leroymerlin.ro/produse/polizoare/1044/polizor-unghiular-bosch-gws-880/61001"


def test_leroymerlin_datadome_200_large_page():
    page = ("<html><head><title>leroymerlin.ro</title></head><body>" + "<div>.</div>" * 1500
            + "<script src='https://ct.captcha-delivery.com/c.js'></script></body></html>")
    with pytest.raises(BlockedError):
        make_store(LeroyMerlinStore, FakeHttp({"leroymerlin.ro": page})).search("flex")
