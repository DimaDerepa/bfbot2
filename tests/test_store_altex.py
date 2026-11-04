from bot.stores.altex import AltexStore, MediaGalaxyStore
from tests.conftest import FakeHttp, make_store

API = {
    "term": "iphone 16 pro",
    "meta": {"items": 2, "size": 48, "page": 1},
    "products": [
        {"id": 1, "sku": "SMTIP16P256DT", "name": "Telefon APPLE iPhone 16 Pro 5G, 256GB, Desert Titanium",
         "url_key": "telefon-apple-iphone-16-pro-5g-256gb-desert-titanium", "price": 5299.99, "regular_price": 6199.99,
         "stock_status": 1, "pickup_is_in_stock": 0, "small_image": "/s/m/smtip16p.jpg", "price_seller_name": "Altex"},
        {"id": 2, "sku": "BATLR03P6BP/10", "name": "Baterii alcaline AAA Philips", "url_key": "baterii-aaa-philips",
         "price": 19.9, "regular_price": 19.9, "stock_status": 0, "pickup_is_in_stock": 0, "price_seller_name": "eMarket SRL"},
    ],
}


def test_altex_search_parsing_and_encoding():
    http = FakeHttp({"fenrir.altex.ro/v2/catalog/search/": API})
    offers = make_store(AltexStore, http).search("iphone 16 pro")
    assert http.calls[0].startswith("https://fenrir.altex.ro/v2/catalog/search/iphone%2016%20pro?")
    assert http.headers_sent[0]["Origin"] == "https://altex.ro"
    a, b = offers
    assert a.url == "https://altex.ro/telefon-apple-iphone-16-pro-5g-256gb-desert-titanium/cpd/SMTIP16P256DT/"
    assert a.price == 5299.99 and a.old_price == 6199.99 and a.in_stock is True and a.seller is None
    assert a.image == "https://lcdn.altex.ro/media/catalog/product/s/m/smtip16p.jpg"
    assert b.url.endswith("/cpd/BATLR03P6BP/10/") and b.old_price is None and b.in_stock is False
    assert b.seller == "eMarket SRL"


def test_altex_blocked_raises():
    import pytest
    from bot.net import BlockedError

    http = FakeHttp({"fenrir.altex.ro": (403, "<html><title>Access Denied</title>Reference #18.abc</html>")})
    with pytest.raises(BlockedError):
        make_store(AltexStore, http).search("tv")


def test_mediagalaxy_brand_header():
    http = FakeHttp({"/v2/catalog/search/": API})
    offers = make_store(MediaGalaxyStore, http).search("iphone")
    assert http.headers_sent[0]["x-brand"] == "mediagalaxy"
    assert offers[0].url.startswith("https://mediagalaxy.ro/")
