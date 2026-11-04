import os

import pytest

from bot.net import BlockedError, Response
from bot.stores.cel import CelStore, _code, slugify
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HTML = open(os.path.join(FIX, "cel_search.html"), encoding="utf-8").read()


class RedirectHttp(FakeHttp):
    """FakeHttp whose responses report another final URL (server-side redirect)."""

    def __init__(self, routes, final):
        super().__init__(routes)
        self.final = final

    def request(self, method, url, **kw):
        r = super().request(method, url, **kw)
        return Response(url=self.final, status=r.status, text=r.text, headers=r.headers)


@pytest.fixture(autouse=True)
def _reset_route_preference():
    CelStore._preferred = None
    yield
    CelStore._preferred = None


def test_cel_search_parsing_and_url():
    http = FakeHttp({"cel.ro/cauta/": HTML})
    store = make_store(CelStore, http)
    offers = store.search("ssd samsung 1tb")
    assert http.calls == ["https://www.cel.ro/cauta/ssd%20samsung%201tb/"]
    by = {o.offer_id: o for o in offers}
    assert list(by) == ["MSQ0NDA2MiU", "NSQ0ODE5MCU", "MSQ0MjIwMCU"]     # card without a price skipped
    a = by["MSQ0NDA2MiU"]
    assert a.url == "https://www.cel.ro/ssd-uri/ssd-samsung-990-pro-1tb-pcie-4-0-nvme-m-2-2280-pMSQ0NDA2MiU-l/"
    assert a.title == "SSD Samsung 990 PRO, 1TB, PCI Express 4.0 x4, NVMe, M.2 2280"
    assert a.price == 569.0 and a.old_price == 699.0 and a.in_stock is True     # del.price before span.price
    assert a.image == "https://s1.cel.ro/images/Products/2023/09/ssd-samsung-990-pro-1tb.jpg"
    b = by["NSQ0ODE5MCU"]
    assert b.price == 449.0 and b.old_price == 1049.0 and b.in_stock is True    # .price_initial, "1.049 lei"
    c = by["MSQ0MjIwMCU"]                                                     # URL without "-l"
    assert c.price == 3299.0 and c.old_price is None and c.in_stock is False  # "3.299" thousands separator
    assert store.last_strategy == "cards"


def test_cel_slug_route_when_percent_route_has_no_cards():
    http = FakeHttp({"cel.ro/cauta/ssd%20samsung%201tb/": "<html><body><div id='bodycode'>?</div></body></html>",
                     "cel.ro/cauta/ssd-samsung-1tb/": HTML})
    store = make_store(CelStore, http)
    offers = store.search("ssd samsung 1tb")
    assert http.calls == ["https://www.cel.ro/cauta/ssd%20samsung%201tb/", "https://www.cel.ro/cauta/ssd-samsung-1tb/"]
    assert len(offers) == 3 and store.last_strategy == "cards:slug"
    # the working route is remembered for the process: the next query starts with the slug form
    http.calls.clear()
    assert len(make_store(CelStore, http).search("ssd samsung 1tb")) == 3
    assert http.calls == ["https://www.cel.ro/cauta/ssd-samsung-1tb/"]
    CelStore._preferred = None
    # a 404 on the %20 form also moves on to the slug form (slug is lowercased, diacritics removed)
    http404 = FakeHttp({"cel.ro/cauta/ssd-samsung-1tb/": HTML, "cel.ro/cauta/": (404, "nu exista")})
    assert len(make_store(CelStore, http404).search("SSD Samsung 1TB")) == 3
    assert http404.calls == ["https://www.cel.ro/cauta/SSD%20Samsung%201TB/", "https://www.cel.ro/cauta/ssd-samsung-1tb/"]


def test_cel_query_encoding_and_slug():
    http = FakeHttp({"cel.ro/cauta/": HTML})
    make_store(CelStore, http).search("tv 55\" qled/oled")
    assert http.calls[0] == "https://www.cel.ro/cauta/tv%2055%22%20qled%2Foled/"
    assert slugify("Mașină de spălat 9 kg") == "masina-de-spalat-9-kg"
    one = FakeHttp({"cel.ro/cauta/": "<html><body>nimic</body></html>"})
    make_store(CelStore, one).search("iphone")
    assert one.calls == ["https://www.cel.ro/cauta/iphone/"]                 # slug == path form: one request


def test_cel_pagination_uses_pager_link():
    http = FakeHttp({"cel.ro/cauta/ssd%20samsung%201tb/2a/": HTML.replace("2190517", "1"), "cel.ro/cauta/": HTML})
    store = make_store(CelStore, http)
    store.search_page("ssd samsung 1tb", 1)
    assert store.search_page("ssd samsung 1tb", 2)
    assert http.calls[1] == "https://www.cel.ro/cauta/ssd%20samsung%201tb/2a/"


def test_cel_errors():
    with pytest.raises(BlockedError):
        make_store(CelStore, FakeHttp({"cel.ro": (403, "<html><title>Access denied</title></html>")})).search("ssd")
    none = FakeHttp({"cel.ro/cauta/": "<html><body><div id='listProducts'>Nu au fost gasite produse</div></body></html>"})
    store = make_store(CelStore, none)
    assert store.search("qwerty zzz") == [] and store.last_strategy == "no-results" and len(none.calls) == 2


def test_cel_product_code_needs_a_real_code():
    assert _code("https://www.cel.ro/ssd-uri/ssd-samsung-990-pro-1tb-pcie-4-0-nvme-m-2-2280-pMSQ0NDA2MiU-l/") == "MSQ0NDA2MiU"
    assert _code("https://www.cel.ro/laptopuri/laptop-apple-macbook-pro/") is None      # not "ro"
    assert _code("https://www.cel.ro/telefoane/huawei-p30/") is None                     # not "30"
    # two code-less URLs ending in "-pro/" must stay two offers (an id "ro" for both would dedupe one away)
    card = ('<div data-pid_prod="{pid}" class="product_data productListing-tot"><h2 class="productTitle">'
            '<a href="https://www.cel.ro/laptopuri/{slug}/" class="product_link"><span>{name}</span></a></h2>'
            '<div class="pret_n"><span class="price" content="{p}">{p}</span><span class="moneda">lei</span></div></div>')
    page = ('<html><body><div id="listProducts">'
            + card.format(pid="11", slug="laptop-apple-macbook-pro", name="Laptop Apple MacBook Pro 14 M4", p="8999")
            + card.format(pid="12", slug="laptop-asus-zenbook-pro", name="Laptop ASUS Zenbook Pro 15 OLED", p="6499")
            + "</div></body></html>")
    offers = make_store(CelStore, FakeHttp({"cel.ro/cauta/": page})).search("laptop pro")
    assert [(o.offer_id, o.price) for o in offers] == [("11", 8999.0), ("12", 6499.0)]   # data-pid_prod fallback


def test_cel_content_attribute_with_thousands_dot_uses_visible_text():
    page = ('<html><body><div id="listProducts"><div data-pid_prod="7" class="product_data productListing-tot">'
            '<h2 class="productTitle"><a href="https://www.cel.ro/laptopuri/laptop-lenovo-ideapad-slim-3-pMSQ5OTk5OSU-l/">'
            '<span>Laptop Lenovo IdeaPad Slim 3 15IAN8</span></a></h2>'
            '<div class="pret_n"><span class="price" content="1.849">1.849</span><span class="moneda">lei</span></div>'
            '</div></div></body></html>')
    o = make_store(CelStore, FakeHttp({"cel.ro/cauta/": page})).search("lenovo")[0]
    assert o.price == 1849.0 and o.offer_id == "MSQ5OTk5OSU"


def test_cel_pager_prefers_the_next_number_over_last_page_arrow():
    page = HTML.replace('<a href="https://www.cel.ro/cauta/ssd%20samsung%201tb/2a/" class="last">',
                        '<a href="https://www.cel.ro/cauta/ssd%20samsung%201tb/7a/" class="last">')
    http = FakeHttp({"cel.ro/cauta/ssd%20samsung%201tb/2a/": HTML, "cel.ro/cauta/": page})
    store = make_store(CelStore, http)
    store.search_page("ssd samsung 1tb", 1)
    store.search_page("ssd samsung 1tb", 2)
    assert http.calls[1] == "https://www.cel.ro/cauta/ssd%20samsung%201tb/2a/"           # not the ">>" (7a) link


def test_cel_redirect_to_home_is_not_a_result_page():
    http = RedirectHttp({"cel.ro/": HTML}, final="https://www.cel.ro/")
    store = make_store(CelStore, http)
    assert store.search("ssd samsung 1tb") == [] and store.last_strategy == "redirect:home"
    assert len(http.calls) == 2 and CelStore._preferred is None                          # both routes tried


def test_cel_product_page():
    page = """<html><head><title>SSD Samsung 990 PRO 1TB - CEL.ro</title></head><body>
    <h1>SSD Samsung 990 PRO, 1TB, PCI Express 4.0 x4, NVMe, M.2 2280</h1>
    <div class="pret_info"><div class="pret_n"><span class="price_initial">699 lei</span>
    <b id="product-price" class="price">569</b> lei</div></div>
    <div class="stoc_list"><strong class="info_stoc in_stoc">In stoc</strong></div></body></html>"""
    url = "https://www.cel.ro/ssd-uri/ssd-samsung-990-pro-1tb-pcie-4-0-nvme-m-2-2280-pMSQ0NDA2MiU-l/"
    o = make_store(CelStore, FakeHttp({"cel.ro/ssd-uri/": page})).product(url)
    assert o.price == 569.0 and o.old_price == 699.0 and o.in_stock is True and o.offer_id == "MSQ0NDA2MiU"
