import os

import pytest

from bot.net import BlockedError
from bot.stores.decathlon import DecathlonStore, decathlon_pid
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "decathlon_search.html"), encoding="utf-8").read()


def test_decathlon_vtmn_cards():
    http = FakeHttp({"decathlon.ro/search": HTML})
    store = make_store(DecathlonStore, http)
    offers = store.search("bicicleta mtb")
    assert http.calls == ["https://www.decathlon.ro/search?Ntt=bicicleta+mtb"]
    assert store.last_strategy == "vtmn-cards"
    by = {o.offer_id: o for o in offers}
    assert list(by) == ["8602357", "8735221", "8667003"]          # gift card without a price dropped
    b = by["8602357"]
    assert b.title == 'ROCKRIDER Bicicletă MTB ST 520 Gri 27,5"'
    assert b.url == "https://www.decathlon.ro/p/bicicleta-mtb-st-520-gri-27-5/_/R-p-344316?mc=8798812"   # mc kept
    assert b.price == 1699.99 and b.old_price == 1999.99 and b.in_stock is None
    assert b.image.startswith("https://contents.mediadecathlon.com/p2397032/")
    e = by["8735221"]
    assert e.price == 3999.0 and e.old_price is None                # whole-lei price "3.999 lei"
    h = by["8667003"]
    assert h.price == 199.99 and h.old_price == 249.99             # unmarked higher vtmn-price = reference price
    assert h.in_stock is False


def test_decathlon_query_encoding_and_pager():
    http = FakeHttp({"from=40": HTML.replace('rel="next"', 'rel="prev"'), "decathlon.ro/search": HTML})
    store = make_store(DecathlonStore, http)
    assert store.search_url("geacă schi copii") == "https://www.decathlon.ro/search?Ntt=geac%C4%83+schi+copii"
    store.search_page("bicicleta mtb", 1)
    store.search_page("bicicleta mtb", 2)
    assert http.calls[1] == "https://www.decathlon.ro/search?Ntt=bicicleta+mtb&from=40&size=40"
    assert store.search_page("bicicleta mtb", 3) == [] and len(http.calls) == 2


def test_decathlon_datadome_block():
    page = '<html><head><title>decathlon.ro</title></head><body><script src="https://ct.captcha-delivery.com/c.js"></script></body></html>'
    with pytest.raises(BlockedError):
        make_store(DecathlonStore, FakeHttp({"decathlon.ro": page})).search("cort")
    with pytest.raises(BlockedError):
        make_store(DecathlonStore, FakeHttp({"decathlon.ro": (403, "<title>Just a moment...</title>")})).search("cort")


def test_decathlon_pid():
    assert decathlon_pid("https://www.decathlon.ro/p/x/_/R-p-344316?mc=8798812") == "344316"
    assert decathlon_pid("https://www.decathlon.ro/sporturi/") is None


def test_decathlon_brand_from_card_text_after_price():
    # no brand class: kutai's "name | price | BRAND | name again" text order
    html = """<div data-supermodelid="8555001"><a class="dpb-product-model-link" href="/p/geaca-schi/_/R-p-300100?mc=8555001&amp;utm=x">
      <span class="vh">Geacă schi caldă 500 Bărbați</span></a>
      <span class="vtmn-price">1.149,99 lei</span><span class="vtmn-price vtmn-price--strikethrough">1.399,99 lei</span>
      <span>-18%</span><span>WEDZE</span><h2>Geacă schi caldă 500 Bărbați</h2><span>(52)</span></div>
      <div data-supermodelid="8555002"><a class="dpb-product-model-link" href="/p/cort/_/R-p-300101?mc=8555002">
      <span class="vh">Cort 2 persoane</span></a><span class="vtmn-price">199 lei</span><span>Livrare gratuită</span></div>"""
    offers = make_store(DecathlonStore, FakeHttp({"decathlon.ro/search": html})).search("geaca schi")
    a, b = offers
    assert a.title == "WEDZE Geacă schi caldă 500 Bărbați" and a.price == 1149.99 and a.old_price == 1399.99
    assert a.url == "https://www.decathlon.ro/p/geaca-schi/_/R-p-300100?mc=8555001" and a.offer_id == "8555001"
    assert b.title == "Cort 2 persoane" and b.price == 199.0               # "Livrare gratuită" is not a brand
