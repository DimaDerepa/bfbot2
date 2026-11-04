import os

import pytest

from bot.net import BlockedError, Response
from bot.stores.pcgarage import PcGarageStore
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HTML = open(os.path.join(FIX, "pcgarage_search.html"), encoding="utf-8").read()


def test_pcgarage_search_parsing_and_url():
    http = FakeHttp({"pcgarage.ro/cauta/": HTML})
    store = make_store(PcGarageStore, http)
    offers = store.search("rtx 4070")
    assert http.calls == ["https://www.pcgarage.ro/cauta/?q=rtx+4070"]
    assert store.last_strategy == "cards"
    by = {o.offer_id: o for o in offers}
    assert list(by) == ["768451", "754210", "771902", "749933"]        # card without a price is skipped
    a = by["768451"]
    assert a.title == "Placa video ASUS GeForce RTX 4070 SUPER DUAL EVO OC 12GB GDDR6X 192-bit"
    assert a.url == "https://www.pcgarage.ro/placi-video/asus/geforce-rtx-4070-super-dual-evo-oc-12gb-gddr6x-192-bit/"
    assert a.price == 3299.99 and a.old_price == 3599.99 and a.in_stock is True   # instalment line ignored
    assert a.image.endswith("/photos/p/rtx4070sdual.jpg")
    b = by["754210"]                                                    # id from adaugaincos-<id>, "de la" prefix
    assert b.price == 2849.0 and b.old_price is None and b.in_stock is True       # "Stoc magazin limitat"
    assert b.image.endswith("rtx4070wf.jpg")                            # data-src wins over the placeholder
    c = by["771902"]
    assert c.price == 4699.99 and c.old_price == 5199.99 and c.in_stock is True   # insupplierstock, "Economisesti" ignored
    d = by["749933"]
    assert d.price == 2999.99 and d.in_stock is False                   # outofstock


def test_pcgarage_query_encoding():
    http = FakeHttp({"pcgarage.ro/cauta/": HTML})
    make_store(PcGarageStore, http).search("ssd 1.5 tb a&b/c")
    assert http.calls[0] == "https://www.pcgarage.ro/cauta/?q=ssd+1.5+tb+a%26b%2Fc"


def test_pcgarage_pagination_follows_pager_link():
    http = FakeHttp({"pcgarage.ro/cauta/pagina2/": HTML.replace("768451", "111111"), "pcgarage.ro/cauta/": HTML})
    store = make_store(PcGarageStore, http)
    assert store.search_page("rtx 4070", 1)
    assert store.search_page("rtx 4070", 2)
    assert http.calls[1] == "https://www.pcgarage.ro/cauta/pagina2/?q=rtx+4070"
    # page 2's ">" link points to page 2 itself -> there is no page 3 and nothing more is requested
    assert store.search_page("rtx 4070", 3) == [] and len(http.calls) == 2


def test_pcgarage_bot_management_loader_is_not_a_block():
    page = HTML.replace('<div id="wrapper_listing_products">', '<div id="wrapper_listing_products" hidden>')
    http = FakeHttp({"pcgarage.ro/cauta/": page})
    assert make_store(PcGarageStore, http).search("rtx 4070")           # jsd/main.js loader present, still parsed


def test_pcgarage_managed_challenge_with_200_raises():
    padding = "<style>" + "a{color:red}" * 700 + "</style>"            # pushes the markers past the core's 6 KB check
    page = ("<!DOCTYPE html><html><head><title>pcgarage.ro</title>" + padding +
            "</head><body><script>window._cf_chl_opt={cType:'managed'};</script>"
            "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=1'></script></body></html>")
    http = FakeHttp({"pcgarage.ro/cauta/": page})
    with pytest.raises(BlockedError):
        make_store(PcGarageStore, http).search("rtx 4070")


def test_pcgarage_403_raises_blocked():
    http = FakeHttp({"pcgarage.ro": (403, "<html><title>Just a moment...</title></html>")})
    with pytest.raises(BlockedError):
        make_store(PcGarageStore, http).search("rtx 4070")


def test_pcgarage_empty_results_fall_back_to_generic():
    page = ('<html><head><title>Cautare: xyz - PC Garage</title></head><body>'
            '<script type="application/ld+json">{"@type":"ItemList","itemListElement":['
            '{"@type":"ListItem","item":{"@type":"Product","name":"Monitor LG UltraGear 27GR75Q","url":"https://www.pcgarage.ro/monitoare-led/lg/27gr75q/",'
            '"offers":{"@type":"Offer","price":"1299.99","priceCurrency":"RON"}}},'
            '{"@type":"ListItem","item":{"@type":"Product","name":"Monitor LG UltraGear 32GS75Q","url":"https://www.pcgarage.ro/monitoare-led/lg/32gs75q/",'
            '"offers":{"@type":"Offer","price":"1799.99","priceCurrency":"RON"}}}]}</script></body></html>')
    store = make_store(PcGarageStore, FakeHttp({"pcgarage.ro/cauta/": page}))
    offers = store.search("xyz")
    assert [o.price for o in offers] == [1299.99, 1799.99]               # plain decimals, not 129999
    assert store.last_strategy.startswith("generic:")


def test_pcgarage_struck_price_inside_price_element_and_real_minimal_card():
    # bf-price-monitor's live-verified minimal card (2026-09) + a struck price nested in p.price
    page = ('<html><head><title>Cautare: rtx 4070 - PC Garage</title></head><body>'
            '<div class="product_box"><div class="product_box_name"><h2><a href="https://www.pcgarage.ro/placi-video/x/">'
            'Placa video RTX 4070</a></h2></div><div class="product_box_price_container"><div class="pb-price">'
            '<p class="price">2.799,99 RON</p></div></div><div class="product_box_availability instock">'
            'Stoc magazin suficient</div></div>'
            '<div class="product_box"><div class="product_box_name"><h2><a href="https://www.pcgarage.ro/placi-video/y/">'
            'Placa video RTX 4070 Ti</a></h2></div><div class="product_box_price_container"><div class="pb-price">'
            '<p class="price"><s>3.599,99 RON</s> 3.299,99 RON</p></div></div>'
            '<div class="product_box_availability outofstock">Stoc epuizat</div></div></body></html>')
    offers = make_store(PcGarageStore, FakeHttp({"pcgarage.ro/cauta/": page})).search("rtx 4070")
    assert [(o.price, o.old_price, o.in_stock) for o in offers] == [(2799.99, None, True), (3299.99, 3599.99, False)]
    assert offers[0].url == "https://www.pcgarage.ro/placi-video/x/" and offers[0].offer_id is None


def test_pcgarage_redirect_to_home_is_not_a_result_page():
    class Redirect(FakeHttp):
        def request(self, method, url, **kw):
            r = super().request(method, url, **kw)
            return Response(url="https://www.pcgarage.ro/", status=r.status, text=r.text, headers=r.headers)

    store = make_store(PcGarageStore, Redirect({"pcgarage.ro/": HTML}))
    assert store.search("rtx 4070") == [] and store.last_strategy == "redirect:home"


def test_pcgarage_product_page_ignores_resealed_offer():
    page = """<html><head><title>Placa video ASUS RTX 4070 - PC Garage</title>
    <meta property="og:title" content="Placa video ASUS GeForce RTX 4070 SUPER DUAL EVO OC"></head><body>
    <h1 id="product_name">Placa video ASUS GeForce RTX 4070 SUPER DUAL EVO OC 12GB GDDR6X 192-bit</h1>
    <div id="pe_unsealed"><div class="pe_unsealed"><meta itemprop="price" content="2799.99">
      <div class="pe_unsealed_price"><span>2.799,99 RON</span></div></div></div>
    <div class="ps_sell_price"><meta itemprop="price" content="3299.99"><span class="price_num">3.299,99 RON</span></div>
    <link itemprop="availability" href="https://schema.org/InStock">
    </body></html>"""
    url = "https://www.pcgarage.ro/placi-video/asus/geforce-rtx-4070-super-dual-evo-oc-12gb-gddr6x-192-bit/"
    o = make_store(PcGarageStore, FakeHttp({"pcgarage.ro/placi-video/": page})).product(url)
    assert o.price == 3299.99 and o.in_stock is True and o.url == url
    assert o.title.startswith("Placa video ASUS GeForce RTX 4070 SUPER")
