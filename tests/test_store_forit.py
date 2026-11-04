import os

import pytest

from bot.net import BlockedError, Response
from bot.stores.forit import ForitStore
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HTML = open(os.path.join(FIX, "forit_search.html"), encoding="utf-8").read()


class RedirectHttp(FakeHttp):
    """FakeHttp whose responses report another final URL (server-side redirect)."""

    def __init__(self, routes, final):
        super().__init__(routes)
        self.final = final

    def request(self, method, url, **kw):
        r = super().request(method, url, **kw)
        return Response(url=self.final, status=r.status, text=r.text, headers=r.headers)


def test_forit_data_product_parsing_and_url():
    http = FakeHttp({"forit.ro/cauta/": HTML})
    store = make_store(ForitStore, http)
    offers = store.search("ssd samsung 1tb")
    assert http.calls == ["https://www.forit.ro/cauta/?q=ssd+samsung+1tb"]
    assert store.last_strategy == "data-product"
    by = {o.offer_id: o for o in offers}
    assert list(by) == ["20416", "21877", "15002"]   # duplicate buttons, price 0, foreign host, bad JSON skipped
    a = by["20416"]
    assert a.url == "https://www.forit.ro/ssd-samsung-990-pro-1tb-pcie-4-0-nvme-m-2-mz-v9p1t0bw-bp20416"
    assert a.title == "SSD Samsung 990 PRO 1TB PCIe 4.0 NVMe M.2, MZ-V9P1T0BW"
    assert a.price == 599.9 and a.old_price == 749.0 and a.in_stock is True    # struck price in card, rate ignored
    assert a.image == "https://www.forit.ro/media/products/20416/ssd-samsung-990-pro.webp"
    b = by["21877"]
    assert b.price == 1049.0 and b.old_price == 1199.0 and b.in_stock is True   # string prices, "la comanda"
    assert b.image == "https://www.forit.ro/media/products/21877/990-evo-plus.webp"
    c = by["15002"]
    assert c.title == 'SSD Samsung 870 EVO 1TB SATA III 2.5"' and c.price == 429.0 and c.in_stock is False


def test_forit_query_encoding_and_pager():
    http = FakeHttp({"forit.ro/cauta/": HTML})
    store = make_store(ForitStore, http)
    store.search_page("rtx 4070 ti/super", 1)
    assert http.calls[0] == "https://www.forit.ro/cauta/?q=rtx+4070+ti%2Fsuper"
    store.search_page("rtx 4070 ti/super", 2)
    assert http.calls[1] == "https://www.forit.ro/cauta/?q=ssd+samsung+1tb&page=2"


def test_forit_generic_fallback_without_data_product():
    page = ('<html><body>'
            '<div class="product"><a href="/placa-video-asus-rtx-4070-dual-12gb-bp30001">Placa video ASUS RTX 4070 Dual 12GB</a>'
            '<span class="price">2.899,00 lei</span><span class="rate">de la 121 lei/luna</span></div>'
            '<div class="product"><a href="/placa-video-msi-rtx-4070-ventus-12gb-bp30002">Placa video MSI RTX 4070 Ventus 12GB</a>'
            '<span class="price">2.799,00 lei</span></div>'
            '<div class="product"><a href="/placa-video-gigabyte-rtx-4070-eagle-12gb-bp30003">Placa video Gigabyte RTX 4070 Eagle 12GB</a>'
            '<span class="price">2.949,00 lei</span></div></body></html>')
    store = make_store(ForitStore, FakeHttp({"forit.ro/cauta/": page}))
    offers = store.search("rtx 4070")
    assert sorted(o.price for o in offers) == [2799.0, 2899.0, 2949.0]
    assert store.last_strategy.startswith("generic:")


def test_forit_single_result_does_not_read_sidebar_or_filters():
    # one product on the page: the card search must stop below <main>/<aside>/filters, otherwise the sidebar's
    # struck promo price and the "Stoc epuizat" filter label become this product's old price / stock
    product = ('{&quot;id&quot;:30001,&quot;title&quot;:&quot;Placa video ASUS RTX 4070 Dual 12GB&quot;,'
               '&quot;price&quot;:2899,&quot;href&quot;:&quot;/placa-video-asus-rtx-4070-dual-12gb-bp30001&quot;}')
    page = ('<html><body><main><h1>Rezultate</h1><aside class="filters"><label><input type="checkbox"> Stoc epuizat'
            '</label><div class="promo"><del>9.999 lei</del></div></aside><div class="products-grid">'
            '<div class="product-item"><h3><a href="/placa-video-asus-rtx-4070-dual-12gb-bp30001">Placa video ASUS RTX'
            ' 4070 Dual 12GB</a></h3><div class="product-price"><s>3.199,00 lei</s> <span>2.899,00 lei</span></div>'
            f"<button data-product='{product}'>Adauga</button></div></div>"
            '<ul class="pagination"><li><span>1</span></li></ul></main></body></html>')
    o = make_store(ForitStore, FakeHttp({"forit.ro/cauta/": page})).search("rtx 4070")[0]
    assert o.price == 2899.0 and o.old_price == 3199.0 and o.in_stock is None and o.offer_id == "30001"


def test_forit_redirect_to_home_is_not_a_result_page():
    http = RedirectHttp({"forit.ro/": HTML}, final="https://www.forit.ro/")
    store = make_store(ForitStore, http)
    assert store.search("ssd samsung 1tb") == [] and store.last_strategy == "redirect:home"


def test_forit_blocked():
    with pytest.raises(BlockedError):
        make_store(ForitStore, FakeHttp({"forit.ro": (429, "slow down")})).search("ssd")
