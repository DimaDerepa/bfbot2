import os

import pytest

from bot.net import BlockedError, Response
from bot.stores.vexio import VexioStore
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HTML = open(os.path.join(FIX, "vexio_search.html"), encoding="utf-8").read()
HOME = ('<html><head><title>Vexio</title></head><body>'
        '<form method="post" action="/newsletter/"><input type="text" name="q"></form>'
        '<form action="/cautare/" method="get" class="search"><input type="hidden" name="submit_search" value="1">'
        '<input type="text" name="keyword" placeholder="Cauta produse"></form></body></html>')


@pytest.fixture(autouse=True)
def _reset_route_cache():
    VexioStore._route = None
    VexioStore._probed = False
    yield
    VexioStore._route = None
    VexioStore._probed = False


def test_vexio_primary_route_parsing():
    http = FakeHttp({"vexio.ro/cauta/?q=": HTML})
    store = make_store(VexioStore, http)
    offers = store.search("samsung galaxy")
    assert http.calls == ["https://www.vexio.ro/cauta/?q=samsung+galaxy"]
    by = {o.offer_id: o for o in offers}
    assert list(by) == ["165432", "170210", "168801"]                    # "Pret la cerere" card skipped
    a = by["165432"]
    assert a.url == "https://www.vexio.ro/smartphone/samsung/165432-galaxy-s24-fe-128gb-8gb-ram-dual-sim-black/"
    assert a.title == "Telefon mobil Samsung Galaxy S24 FE, 128GB, 8GB RAM, Dual SIM, 5G, Black"
    assert a.price == 2799.99 and a.old_price == 3299.99 and a.in_stock is True  # .discounted, instalment ignored
    b = by["170210"]
    assert b.price == 1849.0 and b.old_price is None and b.in_stock is True
    assert b.image == "https://www.vexio.ro/images/products/170210/thumb.jpg"    # data-src over lazy.gif
    c = by["168801"]                                                        # no heading: a.text-center[title]
    assert c.title == "Smartwatch Samsung Galaxy Watch7, 44mm, LTE, Green" and c.in_stock is False
    assert VexioStore._route == "https://www.vexio.ro/cauta/?q={q}"


def test_vexio_struck_first_strong_does_not_drop_the_card():
    page = ('<html><body><article class="product-box"><h2><a href="https://www.vexio.ro/placi-video/msi/171234-rtx-4070-'
            'gaming-x/">Placa video MSI GeForce RTX 4070 Gaming X Slim 12GB</a></h2><div class="price clearfix">'
            '<div class="pull-left"><strong><del>3.299,99 lei</del></strong></div>'
            '<div class="pull-left"><strong>2.799,99 lei</strong></div></div></article></body></html>')
    o = make_store(VexioStore, FakeHttp({"vexio.ro/cauta/?q=": page})).search("rtx 4070")[0]
    assert (o.price, o.old_price, o.offer_id) == (2799.99, 3299.99, "171234")


def test_vexio_discovers_home_page_form_when_guess_404s():
    http = FakeHttp({"vexio.ro/cauta/?q=": (404, "<html>Pagina nu a fost gasita</html>"),
                     "vexio.ro/cautare/?": HTML, "https://www.vexio.ro/": HOME})
    store = make_store(VexioStore, http)
    assert len(store.search("samsung galaxy")) == 3
    assert http.calls == ["https://www.vexio.ro/cauta/?q=samsung+galaxy", "https://www.vexio.ro/",
                          "https://www.vexio.ro/cautare/?submit_search=1&keyword=samsung+galaxy"]
    http.calls.clear()
    make_store(VexioStore, http).search("galaxy s24")                    # route remembered for the process
    assert http.calls == ["https://www.vexio.ro/cautare/?submit_search=1&keyword=galaxy+s24"]


def test_vexio_other_guesses_then_generic_fallback():
    http = FakeHttp({"vexio.ro/cauta/?q=": (404, "nf"), "vexio.ro/cauta/?search=": HTML,
                     "https://www.vexio.ro/": "<html><body>no form</body></html>"})
    assert len(make_store(VexioStore, http).search("samsung galaxy")) == 3
    assert http.calls[-1] == "https://www.vexio.ro/cauta/?search=samsung+galaxy"
    assert VexioStore._route == "https://www.vexio.ro/cauta/?search={q}"


def test_vexio_search_page_without_cards_and_block():
    page = ('<html><head><title>Rezultate cautare: qwerty | Vexio</title></head><body>'
            '<input type="search" name="q" value="qwerty"><p>Nu am gasit produse.</p></body></html>')
    http = FakeHttp({"vexio.ro/cauta/?q=": page})
    store = make_store(VexioStore, http)
    assert store.search("qwerty") == [] and len(http.calls) == 1 and store.last_strategy == "no-results"
    with pytest.raises(BlockedError):                                     # Cloudflare 403: no route probing
        make_store(VexioStore, FakeHttp({"vexio.ro": (403, "<title>Just a moment...</title>")})).search("x")


class RedirectHttp(FakeHttp):
    """FakeHttp that answers `redirects` (url substring -> final url) as a server-side redirect."""

    def __init__(self, routes, redirects):
        super().__init__(routes)
        self.redirects = redirects

    def request(self, method, url, **kw):
        r = super().request(method, url, **kw)
        final = next((f for k, f in self.redirects.items() if k in r.url), r.url)
        return Response(url=final, status=r.status, text=r.text, headers=r.headers)


def test_vexio_redirect_to_home_is_neither_parsed_nor_remembered():
    # the home page also renders article.product-box promotions: a guessed route that redirects there must not
    # turn them into "results", nor be cached as the working route
    home_with_promos = HTML.replace("Rezultate cautare: samsung galaxy", "Vexio - magazin online")
    http = RedirectHttp({"vexio.ro/cauta/?q=": home_with_promos, "vexio.ro/cauta/?search=": HTML,
                         "https://www.vexio.ro/": "<html><body>no form</body></html>"},
                        redirects={"/cauta/?q=": "https://www.vexio.ro/"})
    store = make_store(VexioStore, http)
    offers = store.search("samsung galaxy")
    assert len(offers) == 3 and http.calls[-1] == "https://www.vexio.ro/cauta/?search=samsung+galaxy"
    assert VexioStore._route == "https://www.vexio.ro/cauta/?search={q}"
    VexioStore._route, VexioStore._probed = None, False
    every = RedirectHttp({"vexio.ro": home_with_promos}, redirects={"vexio.ro": "https://www.vexio.ro/"})
    store = make_store(VexioStore, every)
    assert store.search("samsung galaxy") == [] and store.last_strategy == "redirect:home"
    assert VexioStore._route is None


def test_vexio_product_page():
    page = """<html><head><title>Samsung Galaxy S24 FE | Vexio</title></head><body><h1>Telefon mobil Samsung Galaxy S24 FE 128GB</h1>
    <div class="price-box col-xs-10 col-centered"><div class="old_price"><del>3.299,99 lei</del></div>
    <div class="h3 price-value"><span id="price-value">2.799,99 lei</span></div><div class="pvalue">2.353,77 lei</div></div>
    <div class="availability instock">in stoc</div></body></html>"""
    url = "https://www.vexio.ro/smartphone/samsung/165432-galaxy-s24-fe-128gb-8gb-ram-dual-sim-black/"
    o = make_store(VexioStore, FakeHttp({"vexio.ro/smartphone/": page})).product(url)
    assert o.price == 2799.99 and o.old_price == 3299.99 and o.offer_id == "165432" and o.in_stock is True
