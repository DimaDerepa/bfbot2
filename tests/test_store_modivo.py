import json
import os

import pytest

from bot.net import SMALL_PAGE, BlockedError
from bot.stores import registry, store_for_url
from bot.stores.modivo import (
    EpantofiStore, ModivoPlatformStore, ModivoStore, drop_repeated_prefix, modivo_sku, nuxt_unflatten,
)
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
EPANTOFI = open(os.path.join(FIX, "epantofi_search.html"), encoding="utf-8").read()
MODIVO_NUXT = open(os.path.join(FIX, "modivo_search_nuxt.html"), encoding="utf-8").read()


def _redirect_to(http, final):
    orig = http.request

    def request(method, url, **kw):
        resp = orig(method, url, **kw)
        resp.url = final
        return resp
    return request


def test_epantofi_search_cards():
    http = FakeHttp({"epantofi.ro/s/": EPANTOFI})
    store = make_store(EpantofiStore, http)
    offers = store.search("adidas samba")
    assert http.calls == ["https://epantofi.ro/s/adidas%20samba?q=adidas%20samba"]
    assert store.last_strategy == "cards"
    # promo tile (no /p/ link), the duplicate card and the card without a price ("În curând") are dropped
    assert [o.url.rsplit("/", 1)[1] for o in offers] == [
        "pantofi-adidas-samba-super-19099-black-runwht",
        "sneakers-adidas-samba-og-jr0909-alb-0000305636416",
        "sneakers-adidas-samba-xlg-ie1379-negru-0000305176851",
        "ghete-timberland-6-in-premium-boot-tb0a1ztk231-maro-0000302461117",
        "sneakers-adidas-samba-og-kk2277-bej-0000306202085",
    ]
    sup = offers[0]
    assert sup.title == "Sneakers adidas Samba Super 19099 Negru"            # brand kept once
    assert sup.price == 397.9 and sup.old_price == 449.9                    # EU 30-day low beats the 529,90 RRP
    assert sup.offer_id is None and sup.in_stock is None                    # no sku suffix -> URL is the key
    assert sup.image == "https://img.epantofi.ro/media/catalog/product/19099_black_runwht_01.jpg?w=300"
    og = offers[1]
    assert og.title == "Sneakers adidas Samba Og JR0909 Alb" and og.offer_id == "0000305636416"
    assert og.price == 406.9 and og.old_price is None
    xlg = offers[2]
    assert xlg.url == "https://epantofi.ro/p/sneakers-adidas-samba-xlg-ie1379-negru-0000305176851"   # ?ref / #
    assert xlg.image == "https://img.epantofi.ro/media/catalog/product/0000305176851_01.jpg?w=300"   # lazy src
    tb = offers[3]
    assert tb.title == "Ghete Timberland 6 In Premium Boot TB0A1ZTK231 Maro" and tb.price == 1049.9
    assert offers[4].in_stock is False                                      # "Stoc epuizat"
    assert all(o.store == "epantofi" and o.store_name == "ePantofi" for o in offers)


def test_epantofi_follows_the_next_link_only():
    page2 = EPANTOFI.replace("&amp;page=2", "&amp;page=3").replace('rel="next"', 'rel="prev"')
    page2 = page2.replace('class="pagination-next"', 'class="pagination-prev"').replace("›", "‹")
    http = FakeHttp({"page=2": page2, "epantofi.ro/s/": EPANTOFI})
    store = make_store(EpantofiStore, http, max_pages=3)
    store.page_size = 8
    store.search("adidas samba")
    assert http.calls == ["https://epantofi.ro/s/adidas%20samba?q=adidas%20samba",
                          "https://epantofi.ro/s/adidas%20samba?q=adidas%20samba&page=2"]   # page 2: no next link


def test_modivo_nuxt_state_products():
    http = FakeHttp({"modivo.ro/s/": MODIVO_NUXT})
    store = make_store(ModivoStore, http)
    offers = store.search("tommy hilfiger")
    assert http.calls == ["https://modivo.ro/s/tommy%20hilfiger?q=tommy%20hilfiger"]
    assert store.last_strategy == "nuxt-state"
    assert len(offers) == 2                                                 # the PLN-only product is dropped
    boss, tommy = offers
    assert boss.title == "BOSS Trench Cacargo 50472139 Violet Regular Fit"
    assert boss.url == "https://modivo.ro/p/boss-trench-cacargo-50472139-violet-regular-fit"
    assert boss.price == 1499.9 and boss.old_price == 1999.9 and boss.offer_id is None
    assert boss.image.startswith("https://img.modivo.cloud/productcard(")
    assert tommy.title == "Geacă de puf Tommy Hilfiger New York MW0MW35425 Bleumarin"
    assert tommy.price == 899.9 and tommy.old_price is None and tommy.offer_id == "0000304955210"
    assert tommy.store_name == "MODIVO"


def test_query_encoding_matches_encode_uri_component():
    http = FakeHttp({"modivo.ro/s/": MODIVO_NUXT})
    make_store(ModivoStore, http).search("  Nike Air Force 1 '07 ")
    make_store(ModivoStore, http).search("AC/DC & Co (negru)")
    make_store(ModivoStore, http).search("pantofi bărbați")
    assert http.calls == [
        "https://modivo.ro/s/Nike%20Air%20Force%201%20'07?q=Nike%20Air%20Force%201%20'07",
        "https://modivo.ro/s/AC%2FDC%20%26%20Co%20(negru)?q=AC%2FDC%20%26%20Co%20(negru)",
        "https://modivo.ro/s/pantofi%20b%C4%83rba%C8%9Bi?q=pantofi%20b%C4%83rba%C8%9Bi",
    ]


def test_card_variant_with_brand_and_name_elements():
    html = """<html><head><title>MODIVO</title></head><body><a class="header-cart" href="/cos">0,00 Lei</a>
    <section class="listing">
      <article class="product-card" data-test-id="product-card">
        <a href="https://modivo.ro/p/rochie-guess-w4bk12-negru-0000305512345?utm_source=x">
          <img src="https://img.modivo.cloud/a.jpg" alt="Imagine produs">
          <span class="badge-label">Nou</span>
          <p class="product-brand">Guess</p><p class="product-name">Rochie de zi W4BK12 Negru</p>
          <div class="prices"><span class="final">549,90 Lei</span><del>799,90 Lei</del></div>
        </a>
      </article>
      <article class="product-card" data-test-id="product-card">
        <a href="https://modivo.ro/p/sneakers-puma-suede-xl-395205-02-negru-0000305599999">
          <img src="https://img.modivo.cloud/b.jpg" alt="Puma Sneakers Puma Suede XL 395205 02 Negru">
          <div class="price-wrapper"><span>1.299</span> Lei</div>
          <div class="price-wrapper-old"><span>PRP:</span><span>1.599,00 Lei</span></div>
        </a>
      </article>
      <article class="product-card"><a href="https://www.modivo.pl/p/kurtka-0000300000001">x</a>
        <div class="price-wrapper"><span>199,00</span> zł</div></article>
    </section></body></html>"""
    offers = make_store(ModivoStore, FakeHttp({"modivo.ro/s/": html})).search("guess rochie")
    assert [o.offer_id for o in offers] == ["0000305512345", "0000305599999"]   # foreign-shop link skipped
    g, p = offers
    assert g.title == "Guess Rochie de zi W4BK12 Negru"                         # badge "Nou" is not the name
    assert g.url == "https://modivo.ro/p/rochie-guess-w4bk12-negru-0000305512345"
    assert g.price == 549.9 and g.old_price == 799.9
    assert p.title == "Sneakers Puma Suede XL 395205 02 Negru" and p.price == 1299.0 and p.old_price == 1599.0


def test_generic_fallback_on_a_page_without_cards_or_state():
    items = [{"@type": "ListItem", "position": i + 1, "item": {
        "@type": "Product", "name": name, "brand": {"@type": "Brand", "name": "Nike"},
        "url": f"https://epantofi.ro/p/{slug}", "sku": slug.rsplit("-", 1)[1],
        "offers": {"@type": "Offer", "price": price, "priceCurrency": "RON",
                   "availability": "https://schema.org/InStock"}}}
        for i, (name, slug, price) in enumerate([
            ("Nike Sneakers Nike Air Force 1 07 HQ1497 Verde", "sneakers-nike-air-force-1-07-hq1497-verde-0000305356703",
             "507.00"),
            ("Nike Sneakers Nike Air Force 1 LE FV5951 Negru", "sneakers-nike-air-force-1-le-fv5951-negru-0000305364692",
             "549.90")])]
    ld = json.dumps({"@context": "https://schema.org", "@type": "ItemList", "itemListElement": items})
    html = f"""<html><head><title>nike air force | epantofi.ro</title>
    <script type="application/ld+json">{ld}</script></head><body><div id="__nuxt"></div></body></html>"""
    store = make_store(EpantofiStore, FakeHttp({"epantofi.ro/s/": html}))
    offers = store.search("nike air force")
    assert store.last_strategy.startswith("generic:")
    assert [o.price for o in offers] == [507.0, 549.9]
    assert offers[0].url == "https://epantofi.ro/p/sneakers-nike-air-force-1-07-hq1497-verde-0000305356703"


def test_challenge_page_raises_blocked():
    cf = """<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>
    <script>window._cf_chl_opt={cvId:'3',cZone:'modivo.ro'};</script></body></html>"""
    with pytest.raises(BlockedError):
        make_store(ModivoStore, FakeHttp({"modivo.ro": cf})).search("geaca")
    # a big page with a DataDome captcha and no products, which the network layer lets through
    dd = ("<html><head><title>epantofi.ro</title></head><body>" + "<p>.</p>" * (SMALL_PAGE // 8 + 10)
          + '<script src="https://geo.captcha-delivery.com/captcha/?initialCid=x"></script></body></html>')
    with pytest.raises(BlockedError):
        make_store(EpantofiStore, FakeHttp({"epantofi.ro": dd})).search("ghete")


def test_redirect_home_and_redirect_to_product_page():
    http = FakeHttp({"epantofi.ro/s/": "<html><title>epantofi.ro</title><body>home</body></html>"})
    store = make_store(EpantofiStore, http)
    store.http.request = _redirect_to(http, "https://epantofi.ro/")
    assert store.search("x") == [] and store.last_strategy == "redirect:home"

    pdp = """<html><head><title>Sneakers adidas Samba Og JR0909 Alb | epantofi.ro</title>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",
     "name":"Sneakers adidas Samba Og JR0909 Alb","brand":{"@type":"Brand","name":"adidas"},"sku":"0000305636416",
     "offers":{"@type":"Offer","price":"406.90","priceCurrency":"RON","availability":"https://schema.org/InStock"}}</script>
    </head><body><h1>Sneakers adidas Samba Og JR0909 Alb</h1>
    <ul><li data-product-card=""><a class="product-card-link" href="/p/sneakers-adidas-samba-og-b75807-negru-0000200854410">
    <img alt="adidas Sneakers adidas Samba Og B75807 Negru"><div class="price-wrapper"><span>452,90</span> Lei</div></a></li></ul>
    </body></html>"""
    http = FakeHttp({"epantofi.ro/s/": pdp})
    store = make_store(EpantofiStore, http)
    store.http.request = _redirect_to(http, "https://epantofi.ro/p/sneakers-adidas-samba-og-jr0909-alb-0000305636416")
    offers = store.search("JR0909")
    assert store.last_strategy == "product-page" and len(offers) == 1      # the recommendation card is not returned
    assert offers[0].price == 406.9 and offers[0].offer_id == "0000305636416"
    assert offers[0].url == "https://epantofi.ro/p/sneakers-adidas-samba-og-jr0909-alb-0000305636416"


def test_registered_stores():
    reg = registry()
    assert reg["modivo"] is ModivoStore and reg["epantofi"] is EpantofiStore
    assert ModivoPlatformStore not in reg.values()
    assert "fashion" in ModivoStore.categories and "fashion" in EpantofiStore.categories
    assert store_for_url("https://epantofi.ro/p/sneakers-x-0000305636416", reg).key == "epantofi"
    assert store_for_url("https://modivo.ro/p/boss-trench-cacargo-50472139-violet-regular-fit", reg).key == "modivo"


def test_helpers():
    assert modivo_sku("https://epantofi.ro/p/sneakers-adidas-samba-og-jr0909-alb-0000305636416") == "0000305636416"
    assert modivo_sku("https://modivo.ro/p/boss-trench-cacargo-50472139-violet-regular-fit") is None
    assert drop_repeated_prefix("adidas Sneakers adidas Samba Og") == "Sneakers adidas Samba Og"
    assert drop_repeated_prefix("Tommy Hilfiger Geacă Tommy Hilfiger New York") == "Geacă Tommy Hilfiger New York"
    assert drop_repeated_prefix("BOSS Trench Cacargo 50472139") == "BOSS Trench Cacargo 50472139"
    assert drop_repeated_prefix("Puma Puma") == "Puma Puma"                 # nothing would be left after the brand
    payload = [{"data": 1, "seen": 5}, ["ShallowReactive", 2], {"list": 3}, ["Reactive", 4], [6, 7],
               ["Set", 8], {"a": 8}, ["Map", 8, 9], "x", 42]
    assert nuxt_unflatten(payload) == {"data": {"list": [{"a": "x"}, {"x": 42}]}, "seen": ["x"]}
    assert nuxt_unflatten([]) is None and nuxt_unflatten({"a": 1}) is None
