import os

import pytest

from bot.net import BlockedError
from bot.stores.itgalaxy import ItGalaxyStore
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HTML = open(os.path.join(FIX, "itgalaxy_search.html"), encoding="utf-8").read()


def test_itgalaxy_real_cards():
    http = FakeHttp({"itgalaxy.ro/cauta/": HTML})
    store = make_store(ItGalaxyStore, http)
    offers = store.search("ssd 1tb")
    assert http.calls == ["https://www.itgalaxy.ro/cauta/?search=ssd+1tb"]
    by = {o.offer_id: o for o in offers}
    assert list(by) == ["847426", "529727", "873776", "851200"]
    a = by["847426"]
    assert a.title == "SSD WD SN7100 1TB PCIe M.2"                     # visible name, not the shorter a[title]
    assert a.url == "https://www.itgalaxy.ro/ssd/wd/sn7100-1tb-pcie-m-2-847426/"
    assert a.price == 1115.99 and a.old_price is None and a.in_stock is True   # incl. VAT (not 922.3058)
    assert by["529727"].price == 1220.99 and by["529727"].in_stock is True     # "in stoc limitat"
    assert by["873776"].price == 888.99 and by["873776"].in_stock is True      # insupplierstock
    d = by["851200"]
    assert d.price == 1459.9 and d.old_price == 1799.99 and d.in_stock is False
    assert store.last_strategy == "cards"


def test_itgalaxy_query_encoding_and_pager():
    http = FakeHttp({"itgalaxy.ro/cauta/": HTML})
    store = make_store(ItGalaxyStore, http)
    store.search_page("a&b=c d/../x", 1)
    assert http.calls[0] == "https://www.itgalaxy.ro/cauta/?search=a%26b%3Dc+d%2F..%2Fx"
    store.search_page("a&b=c d/../x", 2)
    assert http.calls[1] == "https://www.itgalaxy.ro/cauta/?search=ssd+1tb&page=2"   # taken from the pager


def test_itgalaxy_price_fallbacks():
    card = ('<div id="products-list"><article class="product-box"><h2 class="name"><a href="https://www.itgalaxy.ro/laptop/lenovo/v15-g4-123456/" '
            'title="V15">Laptop Lenovo V15 G4 15.6 FHD Ryzen 5</a></h2><div class="availability">in stoc furnizor</div>'
            '<button class="addtocart-button" data-prod="123456" data-pname="Laptop Lenovo V15 G4 15.6 FHD Ryzen 5" '
            'data-pprice="2349.00" onclick="ecAddToCart(123456, 1941.3223, 1)"></button></article></div>')
    o = make_store(ItGalaxyStore, FakeHttp({"itgalaxy.ro/cauta/": card})).search("lenovo v15")[0]
    assert o.price == 2349.0 and o.offer_id == "123456" and o.in_stock is True


def test_itgalaxy_no_cards_generic_fallback_and_block():
    http = FakeHttp({"itgalaxy.ro/cauta/": "<html><title>Cauta</title><body><p>Nu am gasit produse.</p></body></html>"})
    assert make_store(ItGalaxyStore, http).search("zzzz") == []
    with pytest.raises(BlockedError):
        make_store(ItGalaxyStore, FakeHttp({"itgalaxy.ro": (429, "Too Many Requests")})).search("ssd")


def test_itgalaxy_product_page_reads_own_id_only():
    page = """<html><head><title>SSD WD SN7100 1TB</title></head><body><h1>SSD WD SN7100 1TB PCIe M.2</h1>
    <span id="price-847426" aria-label="Pret 1.115,99 lei">1.115,99 lei</span>
    <div class="availability instock">in stoc suficient</div>
    <div class="related"><span id="price-529727">1.220,99 lei</span></div></body></html>"""
    url = "https://www.itgalaxy.ro/ssd/wd/sn7100-1tb-pcie-m-2-847426/"
    o = make_store(ItGalaxyStore, FakeHttp({"itgalaxy.ro/ssd/": page})).product(url)
    assert o.price == 1115.99 and o.offer_id == "847426" and o.in_stock is True and o.url == url


def test_itgalaxy_product_page_own_id_beats_related_microdata():
    # a related product's schema.org microdata comes first in the page: the generic extractor would take it
    page = """<html><head><title>SSD WD SN7100 1TB</title></head><body>
    <div class="related" itemscope itemtype="https://schema.org/Product"><span itemprop="name">Mouse Logitech G502 Hero</span>
    <div itemprop="offers" itemscope itemtype="https://schema.org/Offer"><meta itemprop="price" content="199.99">
    <meta itemprop="priceCurrency" content="RON"></div></div>
    <h1>SN7100 1TB</h1><div class="availability text-center insupplierstock">in stoc depozit/furnizor</div>
    <button class="addtocart-button" onclick="ecAddToCart(847426, 922.3058, 1)" data-prod="847426"
     data-pname="SSD WD SN7100 1TB PCIe M.2" data-pprice="1115.99">Adauga in cos</button></body></html>"""
    url = "https://www.itgalaxy.ro/ssd/wd/sn7100-1tb-pcie-m-2-847426/"
    http = FakeHttp({"itgalaxy.ro/ssd/": page})
    o = make_store(ItGalaxyStore, http).product(url)
    assert o.price == 1115.99 and o.title == "SSD WD SN7100 1TB PCIe M.2" and o.in_stock is True
    assert len(http.calls) == 1                                          # one request, no re-fetch for the fallback
    # no id-tagged markup -> generic extraction of the same response
    generic = page.replace('data-prod="847426"', 'data-prod="1"')
    o2 = make_store(ItGalaxyStore, FakeHttp({"itgalaxy.ro/ssd/": generic})).product(url)
    assert o2.price == 199.99 and o2.offer_id == "847426" and o2.url == url
