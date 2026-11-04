import os

import pytest

from bot.net import BlockedError
from bot.stores.evomag import EvomagStore
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HTML = open(os.path.join(FIX, "evomag_search.html"), encoding="utf-8").read()
# what the ?sn.q= route returns when results are rendered client-side: the home page with promotions
HOME = ('<html><head><title>evoMAG.ro - Magazin online</title></head><body><div class="index-category-menu">'
        '<a href="/portabile-laptopuri-notebook/">Laptopuri</a></div><div class="nice_product_item"><div class="npi_name">'
        '<a href="/televizoare/samsung-televizor-qled-55-4100001.html">Televizor Samsung QLED 55 inch promo</a></div>'
        '<div class="npi_price"><span class="real_price">2.499,99 Lei</span></div></div></body></html>')


@pytest.fixture(autouse=True)
def _reset_route_preference():
    EvomagStore._preferred = None
    yield
    EvomagStore._preferred = None


def test_evomag_falls_back_to_filter_route_and_parses():
    http = FakeHttp({"evomag.ro/?sn.q=": HOME, "evomag.ro/produse/filtru/cautare:": HTML})
    store = make_store(EvomagStore, http)
    offers = store.search("rtx 4070")
    assert http.calls == ["https://www.evomag.ro/?sn.q=rtx+4070",
                          "https://www.evomag.ro/produse/filtru/cautare:rtx%204070"]
    assert store.last_strategy == "cards:filtru"
    by = {o.offer_id: o for o in offers}
    assert list(by) == ["4185523", "4170021", "4162200"]   # promo outside results, priceless card, duplicate skipped
    a = by["4185523"]
    assert a.url == ("https://www.evomag.ro/componente-pc-gaming-placi-video/gigabyte-placa-video-gigabyte-geforce-"
                     "rtx-4070-super-windforce-oc-12gb-gddr6x-192-bit-4185523.html")      # absolute, no utm
    assert a.title.startswith("Placa video Gigabyte GeForce RTX 4070 SUPER WINDFORCE OC")
    assert a.price == 2999.99 and a.old_price == 3399.99 and a.in_stock is True  # <sup> decimals, rate ignored
    assert a.image == "https://cdn.evomag.ro/i/m/4185523/placa-video-gigabyte.jpg"
    b = by["4170021"]
    assert b.price == 2849.9 and b.old_price == 3149.0 and b.in_stock is True     # "2.849,<sup>90</sup>", 2nd price
    assert by["4162200"].price == 2749.99 and by["4162200"].in_stock is False


def test_evomag_working_route_is_tried_first_next_time():
    http = FakeHttp({"evomag.ro/?sn.q=": HOME, "evomag.ro/produse/filtru/cautare:": HTML})
    make_store(EvomagStore, http).search("rtx 4070")
    http.calls.clear()
    store = make_store(EvomagStore, http)
    assert len(store.search("rtx 4070 super")) == 3 and store.last_strategy == "cards:filtru"
    assert http.calls == ["https://www.evomag.ro/produse/filtru/cautare:rtx%204070%20super"]   # no wasted sn.q hit


def test_evomag_sn_q_home_page_without_menu_marker_is_not_results():
    # /?sn.q= is the home-page path. A home page whose markup lacks .index-category-menu (layout change) must
    # still not report its promotion cards: outside the results section they need a page that echoes the query
    promo_home = HOME.replace('<div class="index-category-menu">', '<div class="menu">')
    http = FakeHttp({"evomag.ro/?sn.q=": promo_home, "evomag.ro/produse/filtru/cautare:": HTML})
    store = make_store(EvomagStore, http)
    offers = store.search("rtx 4070")
    assert store.last_strategy == "cards:filtru" and "4100001" not in {o.offer_id for o in offers}
    # ...while a 2019-style sn.q results page (product_grid, query in the title) is accepted
    grid = ('<html><head><title>Cautare: rtx 4070 - evoMAG</title></head><body><div class="product_grid">'
            '<div class="nice_product_container"><div class="npi_name"><a href="/placi-video/placa-video-rtx-4070-4199999.html">'
            'Placa video Palit GeForce RTX 4070 JetStream</a></div><div class="npi_price"><span class="real_price">'
            '2.699,99 Lei</span></div></div></div></body></html>')
    EvomagStore._preferred = None
    http2 = FakeHttp({"evomag.ro/?sn.q=": grid})
    store2 = make_store(EvomagStore, http2)
    assert [o.offer_id for o in store2.search("rtx 4070")] == ["4199999"] and store2.last_strategy == "cards:sn.q"


def test_evomag_unknown_price_class_never_yields_the_instalment():
    # .real_price renamed: the [class*=price] fallback reads .npi_price, whose "-12%" badge and
    # "sau 125 Lei / luna" instalment line must not become the price
    page = ('<html><body><div class="produse_liste_filter"><div class="nice_product_item"><div class="npi_name">'
            '<a href="/placi-video/placa-video-msi-rtx-4070-gaming-x-4111111.html">Placa video MSI RTX 4070 Gaming X</a>'
            '</div><div class="npi_price"><span class="discount_value">-12%</span><span class="old_price">3.399,99 Lei'
            '</span><div class="npi_rate">sau 125 Lei / luna in 24 rate</div><span class="pret_nou">2.999<sup>99</sup>'
            ' Lei</span></div></div></div></body></html>')
    o = make_store(EvomagStore, FakeHttp({"evomag.ro/?sn.q=": page})).search("rtx 4070")[0]
    assert o.price == 2999.99 and o.old_price == 3399.99 and o.offer_id == "4111111"


def test_evomag_sn_q_route_with_cards_is_enough():
    http = FakeHttp({"evomag.ro/?sn.q=": HTML})
    offers = make_store(EvomagStore, http).search("RTX 4070 Super")
    assert http.calls == ["https://www.evomag.ro/?sn.q=RTX+4070+Super"] and len(offers) == 3


def test_evomag_home_page_on_both_routes_gives_nothing():
    http = FakeHttp({"evomag.ro/": HOME})
    store = make_store(EvomagStore, http)
    assert store.search("rtx 4070") == [] and store.last_strategy == "homepage"
    assert len(http.calls) == 2                                          # home-page promos never become results


def test_evomag_explicit_no_results_stops():
    page = ('<html><body><div class="index-category-menu"></div><div class="produse_liste_filter">'
            '<p>Nu am gasit niciun produs pentru cautarea ta.</p></div></body></html>')
    http = FakeHttp({"evomag.ro/?sn.q=": page})
    store = make_store(EvomagStore, http)
    assert store.search("qwerty") == [] and len(http.calls) == 1 and store.last_strategy == "no-results:sn.q"


def test_evomag_filter_route_404_after_home_page():
    http = FakeHttp({"evomag.ro/?sn.q=": HOME, "evomag.ro/produse/": (404, "<html>404</html>")})
    assert make_store(EvomagStore, http).search("rtx") == []


def test_evomag_pagination_and_block():
    http = FakeHttp({"cautare:rtx%204070/pagina:2": HTML.replace("4185523", "4999999"), "evomag.ro/?sn.q=": HTML})
    store = make_store(EvomagStore, http)
    store.search_page("rtx 4070", 1)
    assert store.search_page("rtx 4070", 2)
    assert http.calls[1] == "https://www.evomag.ro/produse/filtru/cautare:rtx%204070/pagina:2"
    with pytest.raises(BlockedError):
        make_store(EvomagStore, FakeHttp({"evomag.ro": (403, "<title>Attention Required! | Cloudflare</title>")})).search("x")


def test_evomag_product_page():
    page = """<html><body class="htmlbody_product"><h1 class="product_name">Laptop ASUS Vivobook X1504VA, i5-1335U, 16GB, 512GB SSD</h1>
    <div class="price_ajax"><div class="old">2.599,99 Lei</div><div class="pret_rons">2.199<sup class="price_sup">99</sup> Lei</div></div>
    <span itemprop="price" content="2199.99"></span>
    <div class="product-right-credit">Rate de la 99 Lei/luna</div><div class="stock_instocmagazin">In stoc magazin</div></body></html>"""
    url = ("https://www.evomag.ro/portabile-laptopuri-notebook/asus-laptop-asus-vivobook-x1504va-procesor-intel-core-i5-"
           "1335u-12m-cache-up-to-4.60-ghz-15.6-fhd-16gb-512gb-ssd-intel-iris-xe-graphics-roz-4146693.html")
    o = make_store(EvomagStore, FakeHttp({"evomag.ro/portabile": page})).product(url)
    assert o.price == 2199.99 and o.old_price == 2599.99 and o.offer_id == "4146693"
    assert o.in_stock is True and o.title.startswith("Laptop ASUS Vivobook")
