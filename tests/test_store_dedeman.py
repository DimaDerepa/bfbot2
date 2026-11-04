import os

from bot.stores.dedeman import DedemanStore
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "dedeman_search.html"), encoding="utf-8").read()


def test_dedeman_search_url():
    http = FakeHttp({"dedeman.ro/ro/catalogsearch/result/v2": HTML})
    store = make_store(DedemanStore, http)
    store.search("masina de gaurit")
    assert http.calls == ["https://www.dedeman.ro/ro/catalogsearch/result/v2?q=masina+de+gaurit&per_page=96"]
    store.search_page("bormasina", 2)
    assert http.calls[-1] == "https://www.dedeman.ro/ro/catalogsearch/result/v2?q=bormasina&per_page=96&page=2"


def test_dedeman_parsing():
    store = make_store(DedemanStore, FakeHttp({"dedeman.ro": HTML}))
    offers = store.search("masina de gaurit")
    assert store.last_strategy == "magento2-cards"
    by = {o.offer_id: o for o in offers}
    assert set(by) == {"1074060", "1032150", "1090077"}          # card without price skipped
    skil = by["1074060"]
    assert skil.url == ("https://www.dedeman.ro/ro/masina-de-gaurit-si-insurubat-cu-1-acumulator-20-v-2-ah-skil-dd1e3008ac"
                        "-1450-rpm-mandrina-rapida/p/1074060")                  # relative href, tracking query dropped
    assert skil.title.startswith("Masina de gaurit si insurubat, cu 1 acumulator")
    assert skil.price == 279 and skil.old_price == 298 and skil.in_stock is True
    assert skil.image == "https://cdn.dedeman.ro/media/catalog/product/1/0/1074060_1.jpg"
    bosch = by["1032150"]
    assert bosch.price == 1249.5 and bosch.old_price is None and bosch.in_stock is True
    assert bosch.image == "https://cdn.dedeman.ro/media/catalog/product/1/0/1032150.jpg"   # data-src, not the gif
    bit = by["1090077"]
    assert bit.price == 54.9 and bit.in_stock is False                                     # text "54.90 lei /bucată"


def test_dedeman_grid_not_hidden_by_recommendation_widget():
    # a <li class="product-item"> recommendation block must not hide the <div class="product-item"> grid
    widget = ('<div class="block widget block-products-list"><ol class="product-items widget-product-grid">'
              '<li class="product-item"><a class="product-item-link" href="/ro/surubelnita-x/p/1000001">Surubelnita X</a>'
              '<div class="price-box"><span data-price-type="finalPrice" data-price-amount="19.9"></span></div></li>'
              '</ol></div></main>')
    store = make_store(DedemanStore, FakeHttp({"dedeman.ro": HTML.replace("</main>", widget)}))
    offers = store.search("masina de gaurit")
    assert {o.offer_id for o in offers} == {"1074060", "1032150", "1090077"}


def test_dedeman_product_id_from_url():
    store = make_store(DedemanStore, FakeHttp({}))
    assert store.id_from_url("https://www.dedeman.ro/ro/flex-bosch/p/1074060") == "1074060"
    assert store.id_from_url("https://www.dedeman.ro/ro/scaune/c/94") is None
