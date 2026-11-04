"""Regression tests for the generic extraction / price parsing fixes (review findings 6, 7, 20-25, 29 and
the adapter agents' core requests for bot/prices.py)."""
import pytest

from bot.extract import (_is_old_class_token, _num_from, extract_offers, extract_product_page, heuristic_cards,
                         make_soup, offers_from_jsonld, offers_from_microdata)
from bot.prices import PRICE_WITH_CURRENCY_RE, element_price, element_price_text, parse_price


def ld(obj: str) -> str:
    return f'<script type="application/ld+json">{obj}</script>'


def heur(html: str, base: str = "https://shop.ro/cautare?q=x"):
    return {o.url: o for o in heuristic_cards(make_soup(html), base, "shop")}


NAMES = ["Televizor Samsung 55QN90D Neo QLED", "Televizor LG OLED55C4 Smart 139 cm", "Televizor Sony Bravia XR-55A80L"]


def grid(card: str) -> str:
    return "<html><body><div class='grid'>" + "".join(card.format(i=i, n=n) for i, n in enumerate(NAMES)) + "</div></body></html>"


# ---------------------------------------------------------------- prices: machine decimals (findings 20, 29)
@pytest.mark.parametrize("raw,expected", [
    ("1299.9900", 1299.99),
    ("1299.990000", 1299.99),
    ("1299.990", 1299.99),
    ("2499.000000", 2499.0),
    ("2299.0000", 2299.0),
    ("1599.000", 1599.0),
    ("49.000000", 49.0),
    ("12.5000", 12.5),
    ("1299.00000", 1299.0),
    # unchanged Romanian display formats
    ("1.299", 1299.0),
    ("12.999", 12999.0),
    ("4.59999", 4599.99),
    ("1299.99", 1299.99),
    ("12.999.000", 12999000.0),
])
def test_machine_decimals(raw, expected):
    assert parse_price(raw) == expected


def test_machine_decimals_in_structured_fields():
    page = ld('{"@type":"Product","name":"Laptop Lenovo IdeaPad Slim 3","offers":{"@type":"Offer","price":"2499.000000","priceCurrency":"RON"}}')
    assert extract_product_page(page, "https://shop.ro/p", "shop").price == 2499.0
    meta = '<meta property="og:title" content="TV Y"><meta property="product:price:amount" content="1599.000">'
    assert extract_product_page(meta, "https://shop.ro/p", "shop").price == 1599.0
    md = ('<div itemscope itemtype="http://schema.org/Product"><h1 itemprop="name">Televizor Samsung QE55Q60D</h1>'
          '<meta itemprop="price" content="2299.0000"><meta itemprop="priceCurrency" content="RON"></div>')
    assert extract_product_page(md, "https://shop.ro/tv", "shop").price == 2299.0
    assert _num_from("2999.0000") == 2999.0
    assert _num_from("99999999") is None          # same sanity cap as for numbers


# ---------------------------------------------------------------- prices: core requests (a) (b) (c)
def test_sup_after_separator_flanco():
    soup = make_soup('<p><span class="price">2.999,<sup>99</sup> lei</span></p>')
    assert element_price_text(soup.span) == "2.999,99 lei"
    assert element_price(soup.span) == 2999.99
    assert element_price(make_soup("<p>2999.<sup>99</sup> lei</p>").p) == 2999.99
    assert element_price(make_soup("<p><span>2.999,</span><span>99</span> lei</p>").p) == 2999.99
    assert parse_price("2.999,,99 lei") == 2999.99
    # unchanged: eMAG / Altex sup decimals
    assert element_price(make_soup("<p>4.599<sup>99</sup> <span>Lei</span></p>").p) == 4599.99
    assert element_price(make_soup("<p><span>2.199</span><sup>,99</sup> <span>lei</span></p>").p) == 2199.99


@pytest.mark.parametrize("raw,expected", [
    ("2.599 99 Lei", 2599.99),
    ("49 99 Lei", 49.99),
    ("2 599 99 lei", 2599.99),
    ("1299 99 lei", 1299.99),
    ("1 299,99 lei", 1299.99),
    ("1 299 lei", 1299.0),
    ("1299 lei", 1299.0),
    ("12 999 lei", 12999.0),
])
def test_split_decimals_carrefour(raw, expected):
    assert parse_price(raw) == expected


def test_split_decimals_from_elements():
    assert element_price(make_soup("<p><span>2.599</span> <span>99</span> <span>Lei</span></p>").p) == 2599.99


@pytest.mark.parametrize("raw,expected", [
    ("Galaxy S24 999 lei", 999.0),
    ("OLED55C4 899 lei", 899.0),
    ("Pret:89 lei", 89.0),
    ("Pret: 89 lei", 89.0),
    ("PRP: 5.199,99 Lei", 5199.99),
])
def test_currency_regex_does_not_glue_model_numbers(raw, expected):
    assert parse_price(raw) == expected
    m = PRICE_WITH_CURRENCY_RE.search(raw)
    assert m and parse_price(m.group(0)) == expected


# ---------------------------------------------------------------- JSON-LD robustness (finding 24, @type list)
def test_jsonld_raw_control_chars():
    obj = ('{"@type":"Product","name":"Espressor De\'Longhi Magnifica S","sku":"ECAM22",'
           '"description":"Linia 1\r\nLinia 2\tcu tab","offers":{"@type":"Offer","price":"1599.99","priceCurrency":"RON"}}')
    o = extract_product_page(f"<html><head>{ld(obj)}</head></html>", "https://shop.ro/p", "shop")
    assert o and o.price == 1599.99 and o.source == "jsonld"


def test_jsonld_offer_type_list_does_not_crash():
    obj = '{"@type":"Product","name":"Telefon Samsung Galaxy S25","offers":{"@type":["Offer"],"price":"3999","highPrice":"4999","priceCurrency":"RON"}}'
    offers = offers_from_jsonld(make_soup(ld(obj)), "https://shop.ro/", "shop")
    assert offers[0].price == 3999.0 and offers[0].old_price == 4999.0


# ---------------------------------------------------------------- product page (finding 6)
CATEGORY_LD = ld('''{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
 {"@type":"ListItem","position":1,"item":{"@type":"Product","name":"Husa silicon iPhone 15","url":"/husa-iph15","offers":{"@type":"Offer","price":"29.99","priceCurrency":"RON"}}},
 {"@type":"ListItem","position":2,"item":{"@type":"Product","name":"Telefon Samsung Galaxy A15","url":"/a15","offers":{"@type":"Offer","price":"699","priceCurrency":"RON"}}}]}''')


def test_product_page_ignores_itemlist_members():
    # URL watch redirected to a category page: never report a list member's price as the product's
    html = f"<html><head><title>Telefoane mobile</title>{CATEGORY_LD}</head><body></body></html>"
    assert extract_product_page(html, "https://shop.ro/telefoane-mobile/", "shop",
                                requested_url="https://shop.ro/iphone-15-pro-256gb-p123") is None
    # ...also when the category page carries Magento price boxes
    magento = html.replace("<body></body>", '<body><span data-price-type="finalPrice" data-price-amount="29.99"></span></body>')
    assert extract_product_page(magento, "https://shop.ro/telefoane-mobile/", "shop") is None


def test_product_page_microdata_beats_related_jsonld():
    html = ('<html><head>' + ld('{"@type":"ItemList","name":"Produse similare","itemListElement":[{"@type":"ListItem","position":1,'
            '"item":{"@type":"Product","name":"Husa silicon iPhone 15 Pro","url":"/husa","offers":{"@type":"Offer","price":"49.99","priceCurrency":"RON"}}}]}')
            + '</head><body><div itemscope itemtype="http://schema.org/Product"><h1 itemprop="name">Apple iPhone 15 Pro 256GB Natural Titanium</h1>'
            '<div itemprop="offers" itemscope itemtype="http://schema.org/Offer"><meta itemprop="price" content="5499.99">'
            '<meta itemprop="priceCurrency" content="RON"></div></div></body></html>')
    o = extract_product_page(html, "https://shop.ro/iphone-15-pro", "shop")
    assert o.title == "Apple iPhone 15 Pro 256GB Natural Titanium" and o.price == 5499.99


def test_product_page_related_list_before_product():
    html = ld('{"@type":"ItemList","itemListElement":[{"@type":"ListItem","item":{"@type":"Product","name":"Folie sticla iPhone 15 Pro",'
              '"url":"/folie","offers":{"@type":"Offer","price":"39.99","priceCurrency":"RON"}}}]}') + \
        ld('{"@type":"Product","name":"Apple iPhone 15 Pro 256GB","offers":{"@type":"Offer","price":"5499.99","priceCurrency":"RON"}}')
    o = extract_product_page(html, "https://shop.ro/iphone-15-pro-256gb-p123", "shop")
    assert o.title == "Apple iPhone 15 Pro 256GB" and o.price == 5499.99


def test_product_page_prefers_url_match():
    prods = [("Husa iPhone 15 Pro", "/husa", "49.99"), ("Apple iPhone 15 Pro 256GB", "http://www.shop.ro/iphone-15-pro/", "5499.99"),
             ("Folie iPhone 15 Pro", "/folie", "39.99")]
    html = "".join(ld(f'{{"@type":"Product","name":"{n}","url":"{u}","offers":{{"price":"{p}","priceCurrency":"RON"}}}}') for n, u, p in prods)
    o = extract_product_page(html, "https://shop.ro/iphone-15-pro?utm_source=x", "shop")
    assert o.price == 5499.99 and o.url == "https://shop.ro/iphone-15-pro?utm_source=x"
    # several products, none of them this page: a listing, not a product page
    assert extract_product_page(html, "https://shop.ro/altceva", "shop") is None
    # the URL the watch asked for (before a redirect) also counts
    assert extract_product_page(html, "https://shop.ro/redirected", "shop",
                                requested_url="https://shop.ro/iphone-15-pro").price == 5499.99


def test_product_page_microdata_cards_are_not_a_product():
    cards = "".join(f'<div itemscope itemtype="http://schema.org/Product"><a itemprop="url" href="/p/{i}"><span itemprop="name">Produs {i} foarte bun</span></a>'
                    f'<meta itemprop="price" content="{100 + i}"><meta itemprop="priceCurrency" content="RON"></div>' for i in range(3))
    assert extract_product_page(f"<html><body>{cards}</body></html>", "https://shop.ro/categorie/", "shop") is None
    # the listing extractor still returns all of them
    assert len(offers_from_microdata(make_soup(cards), "https://shop.ro/categorie/", "shop")) == 3


def test_product_page_microdata_nested_related_and_webpage_wrapper():
    html = ('<body itemscope itemtype="https://schema.org/WebPage"><div itemscope itemtype="https://schema.org/Product">'
            '<div itemprop="isRelatedTo" itemscope itemtype="https://schema.org/Product"><span itemprop="name">Husa telefon</span>'
            '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer"><meta itemprop="price" content="49.99"></div></div>'
            '<h1 itemprop="name">Telefon Samsung Galaxy S25 Ultra 512GB</h1>'
            '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer"><meta itemprop="price" content="6499.99">'
            '<meta itemprop="priceCurrency" content="RON"><link itemprop="availability" href="https://schema.org/InStock"></div></div></body>')
    o = extract_product_page(html, "https://shop.ro/s25", "shop")
    assert o.title == "Telefon Samsung Galaxy S25 Ultra 512GB" and o.price == 6499.99 and o.in_stock is True


@pytest.mark.parametrize("cur_meta", ['<meta property="product:price:currency" content="EUR">',
                                      '<meta property="og:price:currency" content="USD">'])
def test_product_page_meta_rejects_foreign_currency(cur_meta):
    amount = "og:price:amount" if "og:" in cur_meta else "product:price:amount"
    html = f'<html><head><meta property="og:title" content="Cafetiera X"><meta property="{amount}" content="299.99">{cur_meta}</head></html>'
    assert extract_product_page(html, "https://shop.ro/p", "shop") is None
    ron = html.replace("EUR", "RON").replace("USD", "RON")
    assert extract_product_page(ron, "https://shop.ro/p", "shop").price == 299.99


def test_product_group_variant_by_url():
    obj = ('{"@type":"ProductGroup","name":"Adidasi Nike Air Max 90","hasVariant":['
           '{"@type":"Product","name":"Adidasi Nike Air Max 90 - 42","url":"https://shop.ro/p/am90?variant=42","offers":{"price":"499.99","priceCurrency":"RON"}},'
           '{"@type":"Product","name":"Adidasi Nike Air Max 90 - 43","url":"https://shop.ro/p/am90?variant=43","offers":{"price":"519.99","priceCurrency":"RON"}}]}')
    assert extract_product_page(ld(obj), "https://shop.ro/p/am90?variant=43", "shop").price == 519.99


# ---------------------------------------------------------------- microdata own scope (finding 25)
def test_microdata_title_not_brand_or_seller():
    html = ('<div itemscope itemtype="http://schema.org/Product">'
            '<div itemprop="brand" itemscope itemtype="http://schema.org/Brand"><meta itemprop="name" content="Samsung"></div>'
            '<h1 itemprop="name">Televizor Samsung QLED 55Q80D 138 cm 4K</h1>'
            '<div itemprop="offers" itemscope itemtype="http://schema.org/Offer">'
            '<div itemprop="seller" itemscope itemtype="http://schema.org/Organization"><span itemprop="name">Shop SRL</span></div>'
            '<meta itemprop="price" content="3299.99"><meta itemprop="priceCurrency" content="RON"></div></div>')
    (o,) = offers_from_microdata(make_soup(html), "https://shop.ro/p/tv", "shop")
    assert o.title == "Televizor Samsung QLED 55Q80D 138 cm 4K" and o.price == 3299.99
    assert extract_product_page(html, "https://shop.ro/p/tv", "shop").title == o.title


# ---------------------------------------------------------------- extract_offers: small results (finding 23)
def test_single_structured_result_is_kept():
    one = ld('{"@type":"ItemList","itemListElement":[{"@type":"ListItem","position":1,"item":{"@type":"Product","name":"Consola Sony PlayStation 5 Pro 2TB",'
             '"url":"/ps5-pro","offers":{"@type":"Offer","price":"2999.99","priceCurrency":"RON"}}}]}')
    offers = extract_offers(f"<html><head>{one}</head><body></body></html>", "https://shop.ro/cautare?q=ps5+pro", "shop")
    assert [(o.title, o.price, o.source) for o in offers] == [("Consola Sony PlayStation 5 Pro 2TB", 2999.99, "jsonld")]
    md = ('<div itemscope itemtype="https://schema.org/Product"><a href="/p/9"><span itemprop="name">Consola Sony PlayStation 5 Pro 2TB</span></a>'
          '<meta itemprop="price" content="2999.99"><meta itemprop="priceCurrency" content="RON"></div>')
    assert [o.source for o in extract_offers(md, "https://shop.ro/cautare?q=ps5", "shop")] == ["microdata"]
    assert extract_offers("<html><body><p>Niciun rezultat</p></body></html>", "https://shop.ro/cautare?q=x", "shop") == []


# ---------------------------------------------------------------- heuristic cards: misattribution (finding 7)
IPADS = [("/p/ipad", "Apple iPad", "999"), ("/p/ipad-pro", "Apple iPad Pro 13 M4 256GB", "7.499"),
         ("/p/ipad-air", "Apple iPad Air 11 M2 128GB", "3.299"), ("/p/ipad-mini", "Apple iPad mini 7 128GB Wi-Fi", "2.799")]


def _ipad_card(href, name, price):
    img = '<a href="/p/ipad"><img src="a.jpg"></a>' if href == "/p/ipad" else ""
    return f'<div class="card">{img}<h3><a href="{href}">{name}</a></h3><span class="pret">{price} lei</span></div>'


def test_heuristic_short_title_does_not_steal_neighbour():
    by = heur('<div class="grid">' + "".join(_ipad_card(*c) for c in IPADS) + "</div>", "https://shop.ro/")
    assert by["https://shop.ro/p/ipad"].price == 999 and by["https://shop.ro/p/ipad"].title == "Apple iPad"
    assert by["https://shop.ro/p/ipad-pro"].price == 7499 and by["https://shop.ro/p/ipad-pro"].title == "Apple iPad Pro 13 M4 256GB"


def test_heuristic_rows_of_two():
    rows = ('<div class="grid"><div class="row">' + _ipad_card(*IPADS[0]) + _ipad_card(*IPADS[1]) + '</div><div class="row">'
            + _ipad_card(*IPADS[2]) + _ipad_card(*IPADS[3]) + "</div></div>")
    by = heur(rows, "https://shop.ro/")
    assert {u: o.price for u, o in by.items()} == {"https://shop.ro" + c[0]: parse_price(c[2] + " lei") for c in IPADS}


def test_heuristic_whole_card_link_and_extra_links():
    by = heur(grid("<a class=card href='/p/{i}'><img alt=''><h3>{n}</h3><span>2.{i}99 lei</span></a>"))
    assert [o.title for o in by.values()] == NAMES and by["https://shop.ro/p/1"].price == 2199
    # WooCommerce add-to-cart buttons and a brand link inside the card do not hide the product link
    woo = grid("<li class=product><a href='/produs/{i}/'><h2>{n}</h2></a><span class=price>2.{i}99,00 lei</span>"
               "<a href='?add-to-cart=1{i}' class=button>Adaugă în coș acum</a><a href='/brand/b{i}'>Brand</a></li>")
    assert sorted(o.url for o in heur(woo).values()) == [f"https://shop.ro/produs/{i}/" for i in range(3)]


# ---------------------------------------------------------------- heuristic cards: classes and links (finding 21)
def test_heuristic_tailwind_classes():
    by = heur(grid("<div class=card><a href='/p/{i}'>{n}</a><span class='text-sm line-through'>3.{i}99 lei</span>"
                   "<span class='text-xl font-bold'>2.{i}99 lei</span></div>"))
    assert [(o.price, o.old_price) for o in by.values()] == [(2099, 3099), (2199, 3199), (2299, 3299)]


@pytest.mark.parametrize("card", [
    "<ul><li class='product-list-item'><a href='/p/{i}'>{n}</a><div class='price-box'><span class='price'>2.{i}99 lei</span></div></li></ul>",
    "<div class=item><a href='/p/{i}'>{n}</a><div class='price-holder'><span>2.{i}99 lei</span></div></div>",
])
def test_heuristic_container_classes_are_not_old_price(card):
    assert [o.price for o in heur(grid(card)).values()] == [2099, 2199, 2299]


@pytest.mark.parametrize("prefix", ["/carte/", "/cartuse-imprimanta/", "/cosmetice/"])
def test_heuristic_skip_link_anchored(prefix):
    by = heur(grid("<div class=card><a href='" + prefix + "produs-{i}'>{n}</a><span class=pret>1{i}0 lei</span></div>"))
    assert len(by) == 3


def test_heuristic_skip_links_still_skipped():
    html = grid("<div class=card><a href='/cart?add={i}'>Adauga in cos produsul</a><a href='/wishlist/'>Adauga la favorite</a>"
                "<a href='/p/{i}#reviews'>{n}</a><span>2.{i}99 lei</span></div>")
    assert sorted(heur(html)) == [f"https://shop.ro/p/{i}" for i in range(3)]


@pytest.mark.parametrize("token,old", [
    ("font-bold", False), ("price-holder", False), ("product-list-item", False), ("placeholder", False),
    ("font-regular", False), ("wishlist", False), ("golden", False), ("price-list", False), ("initial", False),
    ("line-through", True), ("has-line-through", True), ("lineThrough", True), ("product-old-price", True),
    ("price__old", True), ("oldPrice", True), ("priceOld", True), ("pret-vechi", True), ("rrp-lp30d", True),
    ("old", True), ("price-was", True), ("list-price", True), ("listPrice", True), ("regular-price", True),
    ("regularPrice", True), ("price-initial", True), ("PRPPrice", True), ("strikethrough", True), ("price-before-discount", True),
])
def test_old_class_tokens(token, old):
    assert _is_old_class_token(token) is old


# ---------------------------------------------------------------- heuristic cards: split price nodes (finding 22)
@pytest.mark.parametrize("price_html,expected", [
    ("<p class='product-new-price'>2.199<sup>99</sup> <span>Lei</span></p>", 2199.99),                   # eMAG
    ("<p class='price'>2.199<sup>99</sup> Lei</p>", 2199.99),
    ("<div class='Price'><span class='Price-int'>2.199</span><sup class='Price-dec'>,99</sup> "
     "<span class='Price-currency'>lei</span></div>", 2199.99),                                            # Altex
    ("<span class='woocommerce-Price-amount amount'><bdi>2.199,00&nbsp;<span>lei</span></bdi></span>", 2199.0),
    ("<span class='price'><span>2.199</span><sup>99</sup> lei</span>", 2199.99),                          # Magento
    ("<span class='price'>2.199,<sup>99</sup> lei</span>", 2199.99),                                      # Flanco
    ("<div class='price'><span>2.199</span><span>99 Lei</span></div>", 2199.99),                          # Carrefour
    ("<div class='price'><span>2.199</span> <span>99</span> <span>Lei</span></div>", 2199.99),
])
def test_heuristic_split_price_nodes(price_html, expected):
    by = heur(grid("<div class='card'><a href='/p/{i}'>{n}</a>" + price_html + "</div>"))
    assert len(by) == 3 and {o.price for o in by.values()} == {expected}


def test_heuristic_split_old_price_and_context():
    card = ("<div class='card'><a href='/p/{i}'>{n}</a><p class='product-old-price'><s>3.199<sup>99</sup> <span>Lei</span></s></p>"
            "<p class='product-new-price'>2.199<sup>99</sup> <span>Lei</span></p><div>sau <b>208</b> <span>lei</span>/luna</div></div>")
    assert {(o.price, o.old_price) for o in heur(grid(card)).values()} == {(2199.99, 3199.99)}
    mixed = "<div class=card><a href='/p/{i}'>{n}</a><div class=prices><del>3.{i}99 lei</del> 2.{i}99 lei</div></div>"
    assert [(o.price, o.old_price) for o in heur(grid(mixed)).values()] == [(2099, 3099), (2199, 3199), (2299, 3299)]
