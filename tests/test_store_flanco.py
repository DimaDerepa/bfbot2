import os

import pytest

from bot.net import BlockedError
from bot.stores._ro_retail import price_text
from bot.stores.flanco import FlancoStore
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "flanco_search.html"), encoding="utf-8").read()


def test_flanco_search_parsing_and_url():
    http = FakeHttp({"flanco.ro/catalogsearch/result/": HTML})
    store = make_store(FlancoStore, http)
    offers = store.search("televizor samsung")
    assert http.calls == ["https://www.flanco.ro/catalogsearch/result/?q=televizor+samsung"]
    assert store.last_strategy == "magento2-cards"
    by = {o.offer_id: o for o in offers}
    # no-price card skipped, duplicate card deduped, "Produse recomandate" widget outside the results ignored
    assert set(by) == {"154321", "160012", "148877", "139001"}
    tv = by["154321"]
    assert tv.title == "Televizor Smart Samsung 65DU8572, 163 cm, Ultra HD 4K, Clasa G"
    assert tv.url == "https://www.flanco.ro/televizor-smart-samsung-65du8572-163-cm-ultra-hd-4k-clasa-g.html"
    assert tv.price == 2999.99 and tv.old_price == 3499.99 and tv.in_stock is True
    assert tv.image.endswith("/154321_1.jpg")
    qled = by["160012"]
    assert qled.price == 6399 and qled.old_price is None and qled.in_stock is True
    assert qled.image == "https://www.flanco.ro/media/catalog/product/cache/e4d6/1/6/160012.jpg"   # lazy data-src
    sb = by["148877"]                                     # text-only prices: "1.249,<sup>90</sup> lei"
    assert sb.price == 1249.90 and sb.old_price == 1499.90 and sb.in_stock is True
    oos = by["139001"]
    assert oos.price == 899.99 and oos.old_price is None and oos.in_stock is False


def test_flanco_pagination_param():
    http = FakeHttp({"flanco.ro/catalogsearch/result/": HTML})
    store = make_store(FlancoStore, http)
    store.search_page("iphone 16 pro", 2)
    assert http.calls == ["https://www.flanco.ro/catalogsearch/result/?q=iphone+16+pro&p=2"]


def test_flanco_cloudflare_blocked():
    page = "<html><head><title>Just a moment...</title></head><body><div id='cf-chl-widget'></div></body></html>"
    with pytest.raises(BlockedError):
        make_store(FlancoStore, FakeHttp({"flanco.ro": (403, page)})).search("tv")
    with pytest.raises(BlockedError):   # challenge served with HTTP 200
        make_store(FlancoStore, FakeHttp({"flanco.ro": page})).search("tv")


def test_flanco_no_results_and_fallback():
    empty = ("<html><head><title>Rezultate pentru: 'xyzzy'</title></head><body><div class='search results'>"
             "<div class='message notice'><div>Căutarea nu a returnat niciun rezultat.</div></div></div>"
             "<ol class='product-items widget-product-grid'><li class='product-item'><a class='product-item-link' "
             "href='https://www.flanco.ro/suport.html'>Suport TV perete</a><span data-price-type='finalPrice' "
             "data-price-amount='99'>99 lei</span></li></ol></body></html>")
    store = make_store(FlancoStore, FakeHttp({"flanco.ro": empty}))
    assert store.search("xyzzy") == [] and store.last_strategy == "no-results"

    ld = ('<html><head><title>Rezultate</title><script type="application/ld+json">{"@type":"ItemList","itemListElement":['
          '{"@type":"ListItem","item":{"@type":"Product","name":"Laptop ASUS Vivobook 15","url":"https://www.flanco.ro/laptop-asus.html?x=1",'
          '"sku":"LAP1","offers":{"@type":"Offer","price":"2499.99","priceCurrency":"RON"}}},'
          '{"@type":"ListItem","item":{"@type":"Product","name":"Laptop Lenovo IdeaPad 3","url":"https://www.flanco.ro/laptop-lenovo.html",'
          '"offers":{"@type":"Offer","price":"1999","priceCurrency":"RON"}}}]}</script></head><body></body></html>')
    store = make_store(FlancoStore, FakeHttp({"flanco.ro": ld}))
    offers = store.search("laptop")
    assert [o.price for o in offers] == [2499.99, 1999.0]
    assert offers[0].url == "https://www.flanco.ro/laptop-asus.html"
    assert store.last_strategy.startswith("generic:")


def test_flanco_product_page():
    pdp = ('<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",'
           '"name":"Televizor Smart Samsung 65DU8572","sku":"65DU8572","offers":{"@type":"Offer","price":2999.99,'
           '"priceCurrency":"RON","availability":"https://schema.org/InStock"}}</script></head><body>'
           '<h1 class="page-title"><span class="base">Televizor Smart Samsung 65DU8572</span></h1>'
           '<div class="product-info-main"><div class="price-box price-final_price" data-product-id="154321">'
           '<span class="special-price"><span data-price-amount="2999.99" data-price-type="finalPrice" class="price-wrapper">'
           '<span class="price">2.999,<sup>99</sup> lei</span></span></span><span class="old-price"><span class="price-label">PRP:</span>'
           '<span data-price-amount="3499.99" data-price-type="oldPrice" class="price-wrapper"><span class="price">3.499,<sup>99</sup> lei'
           '</span></span></span></div></div></body></html>')
    url = "https://www.flanco.ro/televizor-smart-samsung-65du8572-163-cm-ultra-hd-4k-clasa-g.html"
    o = make_store(FlancoStore, FakeHttp({"flanco.ro/televizor": pdp})).product(url)
    assert o.price == 2999.99 and o.old_price == 3499.99 and o.in_stock is True and o.url == url
    # no JSON-LD: Magento price box of .product-info-main
    no_ld = pdp.split("</script>", 1)[1]
    o = make_store(FlancoStore, FakeHttp({"flanco.ro/televizor": "<html>" + no_ld})).product(url)
    assert o.price == 2999.99 and o.old_price == 3499.99 and o.title == "Televizor Smart Samsung 65DU8572"


@pytest.mark.parametrize("text,value", [
    ("2.999,99 lei", 2999.99), ("2.599 99 Lei", 2599.99), ("2.999, 99 lei", 2999.99), ("279.00 lei /bucată", 279.0),
    ("6.399 lei", 6399.0), ("1 299 lei", 1299.0), ("PRP: 2.199,99 lei", 2199.99), ("2.599 99", 2599.99), ("", None),
])
def test_price_text_formats(text, value):
    assert price_text(text) == value
