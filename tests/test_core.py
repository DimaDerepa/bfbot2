import json

import pytest

from bot.config import parse_watch_spec, watch_from_dict
from bot.extract import embedded_json_blobs, extract_offers, extract_product_page, heuristic_cards, make_soup, offers_from_json_blob
from bot.matcher import evaluate, title_matches
from bot.models import Offer, Watch
from bot.prices import element_price, fmt_price, parse_minor_units, parse_price
from bot.text import has_term, norm, query_terms


# ---------------------------------------------------------------- prices
@pytest.mark.parametrize("raw,expected", [
    ("1.299,99 Lei", 1299.99),
    ("1299,99 RON", 1299.99),
    ("1 299,99 lei", 1299.99),
    ("1,299.99", 1299.99),
    ("1299.99", 1299.99),
    ("de la 4.799 lei", 4799.0),
    ("4.59999 Lei", 4599.99),          # flattened eMAG <sup>
    ("12.999.000", 12999000.0),
    ("Pret: 89 lei", 89.0),
    ("0 lei", None),
    ("", None),
    (None, None),
    (4599.9, 4599.9),
    ({"value": "1.234,50"}, 1234.5),
    ("PRP: 5.199,99 Lei", 5199.99),
])
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


def test_minor_units():
    assert parse_minor_units("129999", 2) == 1299.99
    assert parse_minor_units("1500", 0) == 1500.0


def test_element_price_sup():
    soup = make_soup('<p class="product-new-price">4.599<sup>99</sup> <span>Lei</span></p>')
    assert element_price(soup.p) == 4599.99


def test_fmt_price():
    assert fmt_price(4599.0) == "4 599"
    assert fmt_price(4599.99) == "4 599,99"


# ---------------------------------------------------------------- text
def test_norm_units_and_diacritics():
    assert norm("Televizor Smart 55\" OLED, 4K") == "televizor smart 55inch oled 4k"
    assert norm("Mașină de spălat 9 kg") == "masina de spalat 9kg"
    assert norm("iPhone 16 Pro, 256 GB") == "iphone 16 pro 256gb"


def test_has_term_boundaries():
    t = norm("Telefon mobil Apple iPhone 16 Pro, 256GB, 5G, Black Titanium")
    assert has_term(t, "iphone 16 pro")
    assert has_term(t, "256")          # number glued to unit
    assert has_term(t, "256gb")
    assert not has_term(t, "16e")
    assert not has_term(t, "pro max")
    assert not has_term(norm("iPhone 16e 128GB"), "16")
    assert has_term(norm("Placa video RTX-4070 Super"), "rtx 4070")
    assert has_term(norm("Placa video RTX4070"), "rtx 4070")
    assert not has_term(norm("Placa video RTX 40700"), "rtx 4070")
    assert has_term(t, "re:iphone\\s+16")


def test_query_terms_drop_stopwords():
    assert query_terms("aspirator pentru casa cu sac") == ["aspirator", "casa", "sac"]


# ---------------------------------------------------------------- matcher
def W(**kw):
    kw.setdefault("name", kw.get("query", "w"))
    return Watch(**kw)


def O(title, price, old=None, stock=None, store="emag"):
    return Offer(store=store, title=title, url="https://x.ro/" + str(abs(hash(title))), price=price, old_price=old, in_stock=stock)


def test_strict_query_match_and_accessories():
    w = W(query="iphone 16 pro 256gb", exclude=["max"], max_price=5000)
    assert title_matches(w, "Telefon mobil Apple iPhone 16 Pro, 256GB, Desert Titanium")[0]
    assert not title_matches(w, "Telefon mobil Apple iPhone 16 Pro Max, 256GB")[0]
    assert not title_matches(w, "Husa de protectie pentru iPhone 16 Pro 256GB")[0]
    assert not title_matches(w, "Folie sticla iPhone 16 Pro 256gb")[0]
    assert not title_matches(w, "Telefon Apple iPhone 15 Pro 256GB")[0]


def test_accessory_word_only_at_title_start():
    w = W(query="aspirator vertical dyson v15")
    assert title_matches(w, "Aspirator vertical Dyson V15 Detect Absolute, cu suport de perete")[0]
    w2 = W(query="husa iphone 16")
    assert title_matches(w2, "Husa silicon iPhone 16, neagra")[0]
    w3 = W(query="xiaomi smart band 9")
    assert title_matches(w3, "Bratara fitness Xiaomi Smart Band 9, Midnight Black")[0]


def test_price_discount_stock_rules():
    w = W(query="ps5 slim", max_price=2300, min_price=1500, min_discount=10)
    assert evaluate(w, O("Consola Sony PlayStation 5 PS5 Slim Digital", 2199, 2599, True)).ok
    r = evaluate(w, O("Consola Sony PlayStation 5 PS5 Slim Digital", 2299, 2399, True))
    assert r.title_ok and not r.ok and "скидка мала" in r.reasons
    assert not evaluate(w, O("Consola PS5 Slim Digital", 2199, 2599, False)).ok
    assert evaluate(w, O("Consola PS5 Slim Digital", 2199, 2599, None)).ok   # unknown stock passes
    # discount from our own price history when shop shows no old price
    assert evaluate(w, O("Consola PS5 Slim Digital", 2199, None, True), reference_price=2599).ok


def test_must_include_overrides_query_words():
    w = W(query="televizor oled 55", must_include=["oled", "55"], any_of=["lg", "sony"], max_price=5000)
    assert title_matches(w, 'Televizor LG OLED evo 55C4, 139 cm, Smart, 4K')[0] is False  # "55" only inside 55C4
    assert title_matches(w, 'Televizor LG OLED 55", 139 cm, Smart')[0]
    assert not title_matches(w, 'Televizor Samsung OLED 55", Smart')[0]


# ---------------------------------------------------------------- config / spec
def test_parse_watch_spec_compact():
    s = parse_watch_spec('iphone 16 pro 256gb до 4800 >3000 -max -"pro max" +256gb %15 @emag,altex ^cel #electronics !all')
    assert s["query"] == "iphone 16 pro 256gb"
    assert s["max_price"] == 4800 and s["min_price"] == 3000 and s["min_discount"] == 15
    assert s["exclude"] == ["max", "pro max"] and s["must_include"] == ["256gb"]
    assert s["stores"] == ["emag", "altex"] and s["skip_stores"] == ["cel"]
    assert s["categories"] == ["electronics"] and s["in_stock"] is False


def test_parse_watch_spec_variants():
    assert parse_watch_spec("rtx 5070 <3.200")["max_price"] == 3200
    assert parse_watch_spec("rtx 5070 max=3200,50")["max_price"] == 3200.5
    s = parse_watch_spec("https://www.emag.ro/x/pd/ABC/ <2000")
    assert s["urls"] == ["https://www.emag.ro/x/pd/ABC/"] and s["max_price"] == 2000 and "query" not in s
    assert parse_watch_spec("aspirator ?dyson|rowenta|samsung")["any_of"] == ["dyson", "rowenta", "samsung"]
    assert parse_watch_spec("monitor 27 скидка 30%")["min_discount"] == 30


def test_watch_from_dict_aliases():
    w = watch_from_dict({"query": "ps5", "max": 2000, "not": "digital, bundle", "stores": "emag,altex"})
    assert w.max_price == 2000 and w.exclude == ["digital", "bundle"] and w.stores == ["emag", "altex"]
    with pytest.raises(ValueError):
        watch_from_dict({"query": "x", "bogus": 1})


# ---------------------------------------------------------------- generic extraction
JSONLD_LIST = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
 {"@type":"ListItem","position":1,"item":{"@type":"Product","name":"Laptop ASUS Vivobook 15","url":"/laptop-asus/p1","sku":"A1",
   "offers":{"@type":"Offer","price":"2499.99","priceCurrency":"RON","availability":"https://schema.org/InStock"}}},
 {"@type":"ListItem","position":2,"item":{"@type":"Product","name":"Laptop Lenovo IdeaPad 3","url":"/laptop-lenovo/p2",
   "offers":{"@type":"Offer","price":2199,"priceCurrency":"RON","availability":"https://schema.org/OutOfStock"}}}
]}
</script></head><body></body></html>
"""


def test_jsonld_itemlist():
    offers = extract_offers(JSONLD_LIST, "https://shop.ro/cautare?q=laptop", "shop")
    assert [o.title for o in offers] == ["Laptop ASUS Vivobook 15", "Laptop Lenovo IdeaPad 3"]
    assert offers[0].url == "https://shop.ro/laptop-asus/p1" and offers[0].price == 2499.99 and offers[0].in_stock
    assert offers[1].in_stock is False and offers[0].offer_id == "A1"


CARDS = """
<div class="grid">
 <div class="c"><a href="/p/1"><img alt="x"></a><h3><a href="/p/1">Frigider Samsung RB34 No Frost 344 l</a></h3>
   <span class="old">2.999,99 lei</span><span class="now">2.499,99 lei</span><div>sau 208 lei/luna</div><div>În stoc</div></div>
 <div class="c"><h3><a href="/p/2">Frigider LG GBP62 No Frost 384 litri</a></h3><span>3.199 lei</span><div>Stoc epuizat</div></div>
 <div class="c"><h3><a href="/p/3">Frigider Beko RCNA406 No Frost combinat</a></h3><s>2.400,00 Lei</s><b>1.999,00 Lei</b></div>
</div>
"""


def test_heuristic_cards():
    offers = heuristic_cards(make_soup(CARDS), "https://shop.ro/", "shop")
    by = {o.url: o for o in offers}
    assert len(offers) == 3
    a = by["https://shop.ro/p/1"]
    assert a.price == 2499.99 and a.old_price == 2999.99 and a.in_stock is True
    assert by["https://shop.ro/p/2"].in_stock is False and by["https://shop.ro/p/2"].price == 3199
    assert by["https://shop.ro/p/3"].old_price == 2400.0


def test_embedded_state_json():
    html = """<script>window.__INITIAL_STATE__ = {"search":{"items":[
      {"id":11,"name":"Aspirator Dyson V15 Detect","final_price":3299.9,"regular_price":3999.9,"url":"/dyson-v15","in_stock":true},
      {"id":12,"name":"Aspirator Dyson V12 Slim","final_price":"2.499,90","url":"/dyson-v12","in_stock":false}]}};</script>
      <script id="__NEXT_DATA__" type="application/json">{"props":{"x":1}}</script>"""
    blobs = embedded_json_blobs(html)
    assert len(blobs) == 2
    offers = offers_from_json_blob(blobs[0], "https://s.ro/", "s")
    assert [(o.title, o.price, o.old_price, o.in_stock) for o in offers] == [
        ("Aspirator Dyson V15 Detect", 3299.9, 3999.9, True), ("Aspirator Dyson V12 Slim", 2499.9, None, False)]


def test_product_page_meta_fallback():
    html = """<html><head><meta property="og:title" content="Cafetiera De'Longhi Magnifica S">
    <meta property="product:price:amount" content="1599.99"></head></html>"""
    o = extract_product_page(html, "https://shop.ro/p", "shop")
    assert o and o.price == 1599.99 and "Magnifica" in o.title


def test_infer_categories():
    from bot.categorize import infer_categories
    assert infer_categories("placa video rtx 5070") == {"pc"}
    assert "fashion" in infer_categories("adidasi nike air max")
    assert "beauty" in infer_categories("parfum dior sauvage edp")
    assert infer_categories("ceva necunoscut xyz") == set()
    assert "electronics" in infer_categories("iphone 16 pro 256gb")
