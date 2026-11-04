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


# --------------------------------------------------------------------------- real markup (trimmed live capture)

LIVE = open(os.path.join(os.path.dirname(__file__), "fixtures", "carrefour_live.html"), encoding="utf-8").read()

# real "0 rezultate gasite" page (q="rtx 5070"), trimmed; the header menu still holds price-bucket promo links
LIVE_EMPTY = """<!doctype html><html lang="ro"><head><meta charset="utf-8"/>
<title>Rezultatele cautarii pentru:&#039;rtx 5070&#039; | Carrefour Romania</title></head>
<body class="page-products catalogsearch-result-index">
<ul><li class="main"> <a href="https://carrefour.ro/articole-bebelusi/articole-bebe-sub-10-ron?price=3-10" >
<span>Articole bebe sub 10 ron</span> </a> </li>
<li class="secondary"> <a href="https://carrefour.ro/electrocasnice-climatizare/?price=5-20" >
<span>Accesorii pana in 20 lei</span> </a> </li></ul>
<main id="maincontent" class="page-main"><div class="catalogsearch-title-wrapper page-title-wrapper">
<h1 class="page-title" > <span class="base" data-ui-id="page-title-wrapper" >Rezultatele cautarii 'rtx 5070'
<span>0 rezultate gasite</span></span> </h1> </div>
<div class="products wrapper grid products-grid hide"> <ol class="products list"></ol></div>
<p>Graba strica treaba. Se aplica si la tastat. Arunca un ochi sa vezi daca ai scris corect.</p>
</main></body></html>"""


def test_carrefour_live_markup():
    http = FakeHttp({"carrefour.ro/catalogsearch/result/": LIVE})
    store = make_store(CarrefourStore, http)
    offers = store.search("iphone 16 pro")
    assert http.calls == ["https://carrefour.ro/catalogsearch/result/?q=iphone+16+pro"]
    assert store.last_strategy == "carrefour-cards"
    assert [o.offer_id for o in offers] == [
        "19-42509465", "19-42509267", "19-42508882", "19-42509393", "19-14524535", "19-31537241", "19-43502527"]
    for o in offers:                                  # only real product pages, never menu / promo links
        assert o.url.startswith("https://carrefour.ro/produse/") and "?" not in o.url
        assert o.url.endswith(o.offer_id) and o.currency == "RON" and o.in_stock is True and o.seller is None
        assert o.image.startswith("https://cdn-media.carrefour.ro/media/catalog/product/")
        assert "lei" not in o.title.lower()
    by = {o.offer_id: o for o in offers}
    a = by["19-42509267"]
    assert a.title == "Smartphone Apple iPhone 17 Pro, 256GB, 12GB RAM, Dual Sim, 5G, Deep Blue"
    assert a.url == "https://carrefour.ro/produse/smartphone-apple-iphone-17-pro-256gb-12gb-ram-dual-sim-5g-deep-blue-19-42509267"
    assert a.price == 6779.99 and a.old_price is None
    assert by["19-42509465"].title == "Casti wireless Apple AirPods Pro 3, Bluetooth, Alb"
    assert by["19-42509465"].price == 1249.99 and by["19-42509465"].old_price is None   # data-price-amount="1.249,99"
    assert by["19-42508882"].price == 4599.99
    b = by["19-42509393"]                             # struck .price-old "169 99 Lei" next to .price-final
    assert b.title.startswith("Baterie Externa Baseus PicoGo, 10000mAh")
    assert b.price == 135.99 and b.old_price == 169.99
    assert by["19-43502527"].price == 515.99 and by["19-43502527"].old_price == 599.99
    assert by["19-14524535"].price == 4.35 and by["19-14524535"].title == "Arahide in coaja lemnoasa Pro Soft 150g"
    assert by["19-31537241"].price == 129.0 and by["19-31537241"].old_price is None


def test_carrefour_live_no_results_never_returns_menu_links():
    store = make_store(CarrefourStore, FakeHttp({"carrefour.ro": LIVE_EMPTY}))
    assert store.search("rtx 5070") == []
    assert store.last_strategy == "no-results"
