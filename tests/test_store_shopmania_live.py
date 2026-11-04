"""ShopMania search cards parsed from a trimmed REAL page (tests/fixtures/shopmania_live.html)."""
import os

from bot.stores.shopmania import ShopmaniaStore
from tests.conftest import FakeHttp, make_store

LIVE = open(os.path.join(os.path.dirname(__file__), "fixtures", "shopmania_live.html"), encoding="utf-8").read()


def _search():
    store = make_store(ShopmaniaStore, FakeHttp({"shopmania.ro/q-iphone-16-pro": LIVE}))
    offers = store.search("iphone 16 pro")
    return store, offers, {o.offer_id: o for o in offers}


def test_shopmania_live_all_cards_parsed_no_category_links():
    store, offers, by = _search()
    assert store.last_strategy == "cards"
    assert len(offers) == 11
    assert set(by) == {"594535391", "594535385", "95488859", "633039617", "658285127", "637464185", "617066438",
                       "499506751", "413421283", "609085892", "609085898"}
    for o in offers:
        assert o.url.startswith("https://www.shopmania.ro/")
        assert "?q=" not in o.url and "utm_" not in o.url              # category strips /telefoane-mobile?q=.. skipped
        assert o.currency == "RON" and o.old_price is None              # search cards show no struck price
    assert store._pager_get(2) == "https://www.shopmania.ro/q-iphone-16-pro/p2"


def test_shopmania_live_multi_shop_products():
    _, _, by = _search()
    a = by["594535391"]
    assert a.title == "Apple iPhone 16 Pro Max 256GB"
    assert a.url == "https://www.shopmania.ro/telefoane-mobile/p-apple-iphone-16-pro-max-256gb-594535391"
    assert a.price == 5739.0 and a.shop_count == 5 and a.in_stock is None and a.seller is None
    assert a.image == "https://s.cdnshm.com/catalog/ro/t/594535391/apple-iphone-16-pro-max-256gb.jpg"   # not na.svg
    b = by["594535385"]                                     # one listing, no "de la" / "N oferte"
    assert b.title == "Apple iPhone 16 Pro 512GB" and b.price == 8500.0 and b.shop_count is None
    c = by["95488859"]                                      # "de la 53,<sup>99</sup> RON" -- was misread as 5399
    assert c.title == "Cellular Line Pro Urban" and c.price == 53.99 and c.shop_count == 151
    d = by["499506751"]
    assert d.title == "Dyson V15 Detect Absolute" and d.price == 2769.99 and d.shop_count == 6
    assert d.url == "https://www.shopmania.ro/aspiratoare/p-dyson-v15-detect-absolute-499506751"


def test_shopmania_live_single_shop_items():
    _, _, by = _search()
    f = by["633039617"]
    assert f.title == "Folie sticla Ringke Iphone 16 Pro Max 2 buc"
    assert f.price == 139.9 and f.seller == "Macgsm.ro" and f.shop_count == 1 and f.in_stock is True
    assert f.url == ("https://www.shopmania.ro/redirect?d=B2ghBhBKb1BGfwcQMQJid28YODVvV3hZT110ZRQff3wqBXd1RGd3UyoYUUJ8"
                     "GBMRYE8yITEVByAhFBAaaExEcQcRMxs0L2JabCo0FCAIRVR6ahAbfnV1UH56RmEnAXdfAAh9RUJAYQA2MWJPVGInEEFIOxQXIgcN"
                     "Mx00MmVSbnE0AiISE00%3D")                       # data-submit -> /redirect?d= (what the site's JS builds)
    g = by["658285127"]                                     # "In stoc | 22,00 RON": 22 RON is shipping
    assert g.price == 52.99 and g.seller == "TechGSM" and g.in_stock is True
    p = by["413421283"]                                     # "In stoc | 16,90 RON" shipping (was taken as the price)
    assert p.title.startswith("PATONA - Încărcător pentru aspiratoarele DYSON V10")
    assert p.price == 130.0 and p.seller == "Luminam.ro"
    v = by["617066438"]
    assert v.title.startswith("Placa video GeForce RTX 5070 12G VENTUS 2X OC")
    assert v.price == 5882.99 and v.seller == "VEXIO" and v.shop_count == 1
    assert by["637464185"].price == 258.0 and by["637464185"].in_stock is True       # "Disponibil la comanda"
    assert by["609085892"].price == 349.99                                            # "Disponibil cu precomanda"
    out = by["609085898"]                                                             # "Stoc epuizat"
    assert out.price == 299.99 and out.in_stock is False and out.seller == "Dyson.360.ro"
