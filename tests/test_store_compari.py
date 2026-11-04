import os

import pytest

from bot.net import BlockedError, Response
from bot.stores.compari import CompariStore, compari_pid, robots_allowed
from tests.conftest import FakeHttp, make_store

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
LEGACY = open(os.path.join(FIX, "compari_search_legacy.html"), encoding="utf-8").read()
HEUREKA = open(os.path.join(FIX, "compari_search_heureka.html"), encoding="utf-8").read()
PRODUCT = open(os.path.join(FIX, "compari_product.html"), encoding="utf-8").read()


def test_compari_search_url_is_robots_allowed_and_plus_encoded():
    http = FakeHttp({"compari.ro/s/": "<html><body><p>Nu am găsit produse.</p></body></html>"})
    assert make_store(CompariStore, http).search("Căști Sony WH-1000XM5") == []
    assert http.calls == ["https://www.compari.ro/s/?q=C%C4%83%C8%99ti+Sony+WH-1000XM5"]
    assert robots_allowed(http.calls[0])
    assert not robots_allowed("https://www.compari.ro/CategorySearch.php?st=iphone+16")   # Disallow: /*?st=
    assert not robots_allowed("https://www.compari.ro/Jump.php?ProductAttributeId=1")


def test_compari_legacy_listing():
    http = FakeHttp({"compari.ro/s/?q=iphone+16+pro": LEGACY})
    store = make_store(CompariStore, http)
    offers = store.search("iphone 16 pro")
    assert http.calls == ["https://www.compari.ro/s/?q=iphone+16+pro"]
    assert store.last_strategy == "legacy-cards"
    by = {o.offer_id: o for o in offers}
    assert list(by) == ["1100223344", "1100225566", "1100227788"]      # no-price card skipped, duplicate deduped
    a = by["1100223344"]
    assert a.title == "Apple iPhone 16 Pro 256GB"
    assert a.url == "https://telefoane-mobile.compari.ro/apple/iphone-16-pro-256gb-p1100223344/"   # no utm/fragment
    assert a.price == 5299.99 and a.old_price is None and a.in_stock is None
    assert a.shop_count == 14 and a.seller is None
    assert a.image == "https://p1.akcdn.net/thumb/1290455102.apple-iphone-16-pro-256gb.jpg"      # data-lazy-src
    b = by["1100225566"]
    assert b.price == 12989.0 and b.shop_count == 3                                             # NBSP thousands
    assert b.title == "Apple iPhone 16 Pro Max 1TB Desert Titanium"
    s = by["1100227788"]                                                                        # single shop
    assert s.url == "https://huse-telefoane.compari.ro/spigen/ultra-hybrid-magsafe-iphone-16-pro-p1100227788/"
    assert "Jump.php" not in s.url
    assert s.price == 149.9 and s.seller == "emag.ro" and s.shop_count == 1


def test_compari_pagination_follows_only_allowed_next_links():
    page2 = LEGACY.replace("p1100223344", "p1100000001").replace("p1100225566", "p1100000002") \
                  .replace("p1100227788", "p1100000003").replace('rel="next"', 'rel="nofollow"')
    http = FakeHttp({"compari.ro/s/?q=iphone": LEGACY, "telefoane-mobile-c3277/?start=20": page2})
    store = make_store(CompariStore, http)
    assert len(store.search_page("iphone", 1)) == 3
    p2 = store.search_page("iphone", 2)
    assert http.calls[1] == "https://www.compari.ro/telefoane-mobile-c3277/?start=20"
    assert {o.offer_id for o in p2} == {"1100000001", "1100000002", "1100000003"}
    assert store.search_page("iphone", 3) == []                     # no next link on page 2 -> no request
    assert len(http.calls) == 2

    bad = LEGACY.replace("https://www.compari.ro/telefoane-mobile-c3277/?start=20",
                         "https://www.compari.ro/CategorySearch.php?st=iphone&amp;start=20")
    http = FakeHttp({"compari.ro/s/": bad})
    store = make_store(CompariStore, http)
    store.search_page("iphone", 1)
    assert store.search_page("iphone", 2) == [] and len(http.calls) == 1   # robots-disallowed next link ignored


def test_compari_heureka_listing():
    http = FakeHttp({"compari.ro/s/?q=rtx+5070": HEUREKA})
    store = make_store(CompariStore, http)
    offers = store.search("rtx 5070")
    assert store.last_strategy == "heureka-cards"
    assert len(offers) == 2                                          # unavailable item skipped
    a, b = offers
    assert a.title == "ASUS PRIME GeForce RTX 5070 OC 12GB GDDR7"
    assert a.url == "https://www.compari.ro/p/placa-video-asus-prime-geforce-rtx-5070-oc-12gb-gddr7-p1203344556/"
    assert a.offer_id == "1203344556"
    assert a.price == 3149.99                                        # lower bound of "3 149,99 – 3 699,00 lei"
    assert a.shop_count == 9 and a.old_price is None
    assert a.image == "https://im9.cz/iR/importprodukt-orig/abc/rtx5070-prime--mn.jpg"
    assert b.price == 3089.0 and b.old_price == 3499.0 and b.shop_count == 1 and b.offer_id == "1203347788"
    assert store._pager_get(2) == "https://www.compari.ro/s/?q=rtx+5070&page=2"


def test_compari_product_page_offer_list():
    url = "https://telefoane-mobile.compari.ro/apple/iphone-16-pro-256gb-p1100223344/"
    http = FakeHttp({"iphone-16-pro-256gb-p1100223344": PRODUCT})
    o = make_store(CompariStore, http).product(url)
    assert http.calls == [url]
    assert o.title == "Apple iPhone 16 Pro 256GB"
    assert o.price == 5299.99                 # cheapest in-stock offer (5 199 is "Stoc epuizat")
    assert o.seller == "eMAG.ro"              # from the shop logo alt
    assert o.shop_count == 4                  # desktop/mobile duplicate counted once
    assert o.in_stock is True and o.offer_id == "1100223344" and o.url == url
    assert o.image == "https://p1.akcdn.net/full/1290455102.apple-iphone-16-pro-256gb.jpg"
    assert o.store_name == "Compari.ro"


def test_compari_product_page_jsonld_fallback():
    html = ('<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",'
            '"name":"Samsung Galaxy S25 Ultra 512GB","offers":{"@type":"AggregateOffer","lowPrice":"5899.00",'
            '"highPrice":"6999.00","offerCount":"7","priceCurrency":"RON"}}</script></head><body></body></html>')
    url = "https://www.compari.ro/telefoane-mobile-c3277/samsung/galaxy-s25-ultra-512gb-p1150000001/"
    o = make_store(CompariStore, FakeHttp({"galaxy-s25-ultra": html})).product(url)
    assert o.price == 5899.0 and o.shop_count == 7 and o.offer_id == "1150000001" and o.old_price is None


def test_compari_challenge_page_with_200_raises_blocked():
    html = "<html><head><title>Un moment…</title></head><body>" + ("<p>.</p>" * 1500) + \
           "<script>window._cf_chl_opt={cvId:'3',cType:'managed'};</script></body></html>"
    with pytest.raises(BlockedError):
        make_store(CompariStore, FakeHttp({"compari.ro/s/": html})).search("iphone")


def test_compari_403_cloudflare_raises_blocked():
    http = FakeHttp({"compari.ro": (403, "<html><title>Just a moment...</title></html>")})
    with pytest.raises(BlockedError):
        make_store(CompariStore, http).search("iphone")


class _HomeRedirectHttp(FakeHttp):
    def request(self, method, url, **kw):
        r = super().request(method, url, **kw)
        return Response(url="https://www.compari.ro/", status=r.status, text=r.text, headers={})


def test_compari_redirect_to_home_returns_nothing():
    # real markup of the compari.ro home-page "price drop" carousel (snapshot of 2024-08-25, XSmeets/mep-data)
    home = ('<html><body><div class="ak-carousel pricedrop-home"><div class="item"><div class="content">'
            '<a href="https://www.compari.ro/aspiratoare-c3170/makita/dcl281fzb-p496740903/" title="Makita DCL281FZB">'
            '<img src="https://p1.akcdn.net/thumb/1.makita-dcl281fzb.jpg" alt="Makita DCL281FZB"></a></div>'
            '<a class="name" href="https://www.compari.ro/aspiratoare-c3170/makita/dcl281fzb-p496740903/" '
            'title="Makita DCL281FZB"><span>476,80 RON</span>Makita DCL281FZB</a></div>'
            '<div class="item"><div class="content"><a href="https://mixer-dj.compari.ro/boss/gigcaster-8-gcs-8-p952304121/"'
            ' title="BOSS Gigcaster 8 (GCS-8)"><span class="pricedrop-badge">-39%</span></a></div>'
            '<a class="name" href="https://mixer-dj.compari.ro/boss/gigcaster-8-gcs-8-p952304121/" '
            'title="BOSS Gigcaster 8 (GCS-8)"><span>de la 1 989,00 RON</span>BOSS Gigcaster 8 (GCS-8)</a></div>'
            '</div></body></html>')
    store = make_store(CompariStore, _HomeRedirectHttp({"compari.ro/s/": home}))
    assert store.search("makita") == [] and store.last_strategy == "redirect-home"


class _RedirectHttp(FakeHttp):
    def __init__(self, routes, final):
        super().__init__(routes)
        self.final = final

    def request(self, method, url, **kw):
        r = super().request(method, url, **kw)
        return Response(url=self.final, status=r.status, text=r.text, headers={})


def test_compari_redirect_to_category_subdomain_root_is_a_listing():
    # research: the search may redirect to a category listing, incl. a category SUBDOMAIN (path "/") - not home
    store = make_store(CompariStore, _RedirectHttp({"compari.ro/s/": LEGACY}, "https://telefoane-mobile.compari.ro/"))
    offers = store.search("iphone 16 pro")
    assert store.last_strategy == "legacy-cards" and len(offers) == 3


def test_compari_search_landing_on_product_page_reads_offer_list():
    url = "https://telefoane-mobile.compari.ro/apple/iphone-16-pro-256gb-p1100223344/"
    store = make_store(CompariStore, _RedirectHttp({"compari.ro/s/": PRODUCT}, url + "?utm_source=s"))
    (o,) = store.search("apple iphone 16 pro 256gb")
    assert store.last_strategy == "product-page"
    assert o.url == url and o.offer_id == "1100223344" and o.price == 5299.99 and o.seller == "eMAG.ro"
    assert o.shop_count == 4 and o.store_name == "Compari.ro"


def test_compari_single_shop_generic_label_is_not_a_seller():
    html = LEGACY.replace(">emag.ro</a>", ">Vezi oferta</a>")
    by = {o.offer_id: o for o in make_store(CompariStore, FakeHttp({"compari.ro/s/": html})).search("iphone")}
    s = by["1100227788"]
    assert s.seller is None and s.shop_count == 1          # offer-num links out through Jump.php: one shop


def test_compari_jump_only_cards_keep_distinct_keys():
    # single-shop cards with no data-akpid / ak-info: the only link is the outbound Jump.php (query stripped by
    # canonical_url), so without a fallback id every such card would collapse into one
    card = ('<div class="product-box clearfix"><div class="name ulined-link"><h2><a href="https://www.compari.ro/Jump.php?'
            '{q}&amp;ProductType=product" title="{t}">{t}</a></h2></div><div class="top-right"><a class="price" '
            'href="https://www.compari.ro/Jump.php?{q}">{p} RON</a><a class="offer-num" href="https://www.compari.ro/'
            'Jump.php?{q}&amp;ProductType=shop">{s}</a></div></div>')
    html = "<html><body>" + "".join(card.format(q=q, t=t, p=p, s=s) for q, t, p, s in (
        ("ProductAttributeId=555001", "Cablu USB-C Anker 1 m", "39,90", "anker-store.ro"),
        ("ProductAttributeId=555002", "Cablu USB-C Anker 2 m", "49,90", "emag.ro"),
        ("ref=abc", "Încărcător Anker Nano 30W", "99,00", "flanco.ro"),
        ("ref=def", "Încărcător Anker Nano 65W", "1 199,00", "altex.ro"))) + "</body></html>"
    offers = make_store(CompariStore, FakeHttp({"compari.ro/s/": html})).search("anker")
    assert [o.offer_id for o in offers] == ["pa555001", "pa555002", "t-incarcator-anker-nano-30w",
                                            "t-incarcator-anker-nano-65w"]
    assert offers[0].url == "https://www.compari.ro/Jump.php?ProductAttributeId=555001"        # never fetched
    assert [o.seller for o in offers] == ["anker-store.ro", "emag.ro", "flanco.ro", "altex.ro"]
    assert offers[3].price == 1199.0 and all(o.shop_count == 1 for o in offers)


def test_compari_lazy_gif_placeholder_is_not_the_image():
    html = LEGACY.replace(' data-lazy-src="https://p1.akcdn.net/thumb/1290455102.apple-iphone-16-pro-256gb.jpg"', "")
    by = {o.offer_id: o for o in make_store(CompariStore, FakeHttp({"compari.ro/s/": html})).search("iphone")}
    assert by["1100223344"].image is None


def test_compari_heureka_struck_price_inside_price_box_and_url_without_pid():
    html = ('<html><body><ul class="c-product-list">'
            '<li class="c-product-list__item" data-testid="product-list-item"><article class="c-product">'
            '<h3 class="c-product__title"><a class="c-product__link" data-testid="product-title-link" '
            'href="/p/placa-video-msi-geforce-rtx-5070-ventus-2x/?from=search">MSI GeForce RTX 5070 Ventus 2X</a></h3>'
            '<div data-testid="product-price"><a class="c-product__price" href="/p/placa-video-msi-geforce-rtx-5070-ventus-2x/">'
            '<del>3 999,00 lei</del> de la 3 089,00 lei</a></div><span class="c-product__shops">în 4 magazine</span>'
            '</article></li>'
            '<li class="c-product-list__item" data-testid="product-list-item"><article class="c-product">'
            '<h3 class="c-product__title"><a class="c-product__link" data-testid="product-title-link" '
            'href="/p/placa-video-gigabyte-rtx-5070-windforce/">Gigabyte GeForce RTX 5070 WINDFORCE OC SFF 12GB</a></h3>'
            '<div data-testid="product-price"><span class="c-product__price">2 949,90 lei</span></div>'
            '</article></li></ul></body></html>')
    store = make_store(CompariStore, FakeHttp({"compari.ro/s/": html}))
    a, b = store.search("rtx 5070")
    assert a.price == 3089.0 and a.old_price == 3999.0 and a.shop_count == 4      # not the struck 3 999
    assert a.url == "https://www.compari.ro/p/placa-video-msi-geforce-rtx-5070-ventus-2x/" and a.offer_id is None
    assert b.price == 2949.9 and b.old_price is None and b.shop_count is None
    assert a.key != b.key                                                         # keyed by canonical URL


def test_compari_product_page_mobile_duplicates_with_other_shop_label():
    # the mobile copy of the Flanco row names the shop by its logo ("flanco.ro") instead of "Flanco"
    html = PRODUCT.replace('<div class="shopname"><span itemprop="name">Flanco</span></div>\n    </div>',
                           '<div class="col-logo"><img src="x.png" alt="flanco.ro"></div>\n    </div>')
    assert html != PRODUCT
    url = "https://telefoane-mobile.compari.ro/apple/iphone-16-pro-256gb-p1100223344/"
    o = make_store(CompariStore, FakeHttp({"p1100223344": html})).product(url)
    assert o.shop_count == 4 and o.price == 5299.99


def test_compari_passive_cloudflare_script_is_not_a_block():
    # normal (unparseable / empty) page between core's 60 KB block-sniff limit and the adapter's 120 KB one, carrying
    # Cloudflare's passive bot-detection script: must reach the generic fallback, not be reported as BLOCKED
    html = ("<html><head><title>Rezultatele căutării - Compari.ro</title></head><body><p>Nu am găsit produse.</p>"
            + "<p>Text informativ despre categorie.</p>" * 2500
            + "<script>(function(){var a=document.createElement('script');"
              "a.src='/cdn-cgi/challenge-platform/h/b/scripts/jsd/5eaf848a0845/main.js';})();</script></body></html>")
    assert 60_000 < len(html) < 120_000
    store = make_store(CompariStore, FakeHttp({"compari.ro/s/": html}))
    assert store.search("xyz") == []


def test_compari_pid():
    assert compari_pid("https://mixer-dj.compari.ro/boss/gigcaster-8-gcs-8-p952304121/") == "952304121"
    assert compari_pid("https://www.compari.ro/aspiratoare-c3170/") is None
