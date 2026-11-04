"""Regression tests for the matching / config / text review findings (8-12, 15, 27, 28, 30-32)."""
import pytest

from bot.categorize import infer_categories
from bot.config import LATIN_HINT, load_config, parse_watch_spec, watch_from_dict
from bot.matcher import evaluate, title_matches
from bot.models import Offer, Watch
from bot.text import has_term, norm, slug


def W(**kw):
    kw.setdefault("name", kw.get("query", "w"))
    return Watch(**kw)


def tm(query, title, **kw):
    return title_matches(W(query=query, **kw), title)[0]


# ------------------------------------------------------------------ 8: bare "max"/"min" are model words, not prices
def test_bare_max_is_part_of_the_model_name():
    assert parse_watch_spec("iphone 15 pro max 256 до 6000") == {"query": "iphone 15 pro max 256", "max_price": 6000.0}
    assert parse_watch_spec("iphone 16 pro max 512") == {"query": "iphone 16 pro max 512"}
    assert parse_watch_spec("galaxy s24 ultra min 5000") == {"query": "galaxy s24 ultra min 5000"}
    # the documented English forms still work
    assert parse_watch_spec("iphone 16 max=4800 min=3000") == {"query": "iphone 16", "max_price": 4800.0, "min_price": 3000.0}
    w = watch_from_dict(parse_watch_spec("iphone 15 pro max 256 до 6000"), origin="telegram")
    plain = Offer(store="emag", title="Apple iPhone 15 Pro, 128GB, Natural Titanium", url="https://x.ro/1", price=4999)
    assert not evaluate(w, plain).ok


# ------------------------------------------------------------------ 9: Russian price formats
@pytest.mark.parametrize("text,key,value", [
    ("iphone 16 pro до 4 800", "max_price", 4800.0),
    ("iphone 16 pro <3 200", "max_price", 3200.0),
    ("iphone 16 pro <3,200", "max_price", 3200.0),
    ("iphone 16 pro до 5к", "max_price", 5000.0),
    ("iphone 16 pro до 5k", "max_price", 5000.0),
    ("iphone 16 pro до 5 тыс", "max_price", 5000.0),
    ("iphone 16 pro до 5 тыс.", "max_price", 5000.0),
    ("iphone 16 pro <4,5k", "max_price", 4500.0),
    ("iphone 16 pro от 2 000", "min_price", 2000.0),
    ("iphone 16 pro >2 000", "min_price", 2000.0),
    ("iphone 16 pro до 12 500,50", "max_price", 12500.5),
    ("iphone 16 pro ≤ 3000", "max_price", 3000.0),
])
def test_russian_price_formats(text, key, value):
    s = parse_watch_spec(text)
    assert s[key] == value
    assert s["query"] == "iphone 16 pro"


def test_digit_groups_are_joined_only_after_a_price_operator():
    assert parse_watch_spec("galaxy s24 128 до 3000") == {"query": "galaxy s24 128", "max_price": 3000.0}
    assert parse_watch_spec("iphone 16 256 <5000") == {"query": "iphone 16 256", "max_price": 5000.0}
    assert parse_watch_spec("до 4 500") == {"max_price": 4500.0}           # /set 1 до 4 500 no longer sets query "500"
    # existing formats keep their meaning
    assert parse_watch_spec("rtx 5070 <3.200")["max_price"] == 3200
    assert parse_watch_spec("rtx 5070 max=3200,50")["max_price"] == 3200.5
    assert parse_watch_spec("rtx 5070 <1.299,99")["max_price"] == 1299.99
    assert parse_watch_spec("tv 4k до 3000") == {"query": "tv 4k", "max_price": 3000.0}   # "4k" outside a price stays a word


def test_price_operator_without_number_is_an_error_not_a_query_word():
    for text in ("iphone до", "iphone до abc", "iphone <abc", "iphone скидка", "rtx max=abc"):
        with pytest.raises(ValueError):
            parse_watch_spec(text)


# ------------------------------------------------------------------ 30: leftovers no longer become mandatory words
def test_currency_words_are_consumed():
    for cur in ("lei", "лей", "ron", "RON", "Lei"):
        assert parse_watch_spec(f"iphone 16 pro до 4800 {cur}") == {"query": "iphone 16 pro", "max_price": 4800.0}
    assert parse_watch_spec("iphone 16 pro <4800lei") == {"query": "iphone 16 pro", "max_price": 4800.0}
    assert parse_watch_spec("tv от 2 000 до 3 500 лей") == {"query": "tv", "min_price": 2000.0, "max_price": 3500.0}
    assert parse_watch_spec("harry potter ron weasley")["query"] == "harry potter ron weasley"


def test_bare_number_with_currency_asks_for_the_price_syntax():
    with pytest.raises(ValueError, match="до 4800"):
        parse_watch_spec("iphone 16 pro 4800 lei")


def test_store_list_with_space_after_comma():
    s = parse_watch_spec("rtx 5070 @emag, altex ^cel ,vexio #pc, electronics")
    assert s == {"query": "rtx 5070", "stores": ["emag", "altex"], "skip_stores": ["cel", "vexio"],
                 "categories": ["pc", "electronics"]}


def test_minus_number_is_an_exclusion():
    s = parse_watch_spec("iphone 16 pro -128")
    assert s == {"query": "iphone 16 pro", "exclude": ["128"]}
    w = watch_from_dict(s, origin="telegram")
    assert not title_matches(w, "Telefon mobil Apple iPhone 16 Pro, 128GB, 5G")[0]
    assert title_matches(w, "Telefon mobil Apple iPhone 16 Pro, 256GB, 5G")[0]


@pytest.mark.parametrize("text", ["aspirator dyson скидка от 25%", "aspirator dyson скидка 25 %",
                                  "aspirator dyson от 25%", "aspirator dyson >25%", "aspirator dyson -25%",
                                  "aspirator dyson скидка: 25"])
def test_discount_forms(text):
    assert parse_watch_spec(text) == {"query": "aspirator dyson", "min_discount": 25.0}


# ------------------------------------------------------------------ 31: smart / Russian quotes
@pytest.mark.parametrize("q", ['-"pro max"', "-“pro max”", "-«pro max»", "-„pro max“"])
def test_smart_quotes_in_two_word_exclude(q):
    s = parse_watch_spec(f"iphone 16 pro {q}")
    assert s == {"query": "iphone 16 pro", "exclude": ["pro max"]}
    w = watch_from_dict(s, origin="telegram")
    assert title_matches(w, "Apple iPhone 16 Pro, 256GB, Desert Titanium")[0]
    assert not title_matches(w, "Apple iPhone 16 Pro Max, 256GB")[0]
    assert parse_watch_spec("iphone 16 +«256 gb»")["must_include"] == ["256 gb"]


# ------------------------------------------------------------------ 10: trademark signs
def test_trademark_signs_do_not_glue_letters():
    assert norm("Placa video MSI GeForce RTX™ 5070 12G") == "placa video msi geforce rtx 5070 12g"
    assert tm("rtx 5070", "Placa video MSI GeForce RTX™ 5070 12G VENTUS 2X OC")
    assert tm("ryzen 7 7800x3d", "Procesor AMD Ryzen™ 7 7800X3D, 4.2GHz")
    assert tm("intel core i7 14700k", "Procesor Intel® Core™ i7-14700K, 3.4GHz")
    assert tm("dyson v15", "Aspirator Dyson℠ V15 Detect©")


def test_inch_double_prime():
    assert norm("Televizor Samsung 55″ QLED") == "televizor samsung 55inch qled"
    assert tm("televizor 55inch", "Televizor Samsung 55″ QLED")


# ------------------------------------------------------------------ 11: Cyrillic
@pytest.mark.parametrize("text", ["пылесос дайсон до 3000", "айфон 16 про", "iphone 16 +чёрный", "iphone 16 -чехол",
                                  "aspirator ?dyson|ровента", "husa для iphone"])
def test_cyrillic_words_are_refused_with_a_hint(text):
    with pytest.raises(ValueError) as e:
        parse_watch_spec(text)
    assert LATIN_HINT in str(e.value)


@pytest.mark.parametrize("d", [{"query": "пылесос дайсон"}, {"query": "dyson", "must_include": ["пылесос"]},
                               {"query": "dyson", "any_of": "v15, вертикальный"}, {"query": "dyson", "exclude": ["фильтр"]}])
def test_watch_from_dict_refuses_cyrillic(d):
    with pytest.raises(ValueError, match="латиницей"):
        watch_from_dict(d, origin="telegram")


def test_cyrillic_name_is_fine_and_russian_filler_words_are_consumed():
    w = watch_from_dict({"name": "Пылесос Дайсон", "query": "aspirator dyson"})
    assert w.name == "Пылесос Дайсон"
    assert parse_watch_spec("aspirator dyson до 3000 лей скидка от 20%") == {
        "query": "aspirator dyson", "max_price": 3000.0, "min_discount": 20.0}


def test_query_without_latin_words_matches_nothing():
    # a watch stored by an older version (validate=False keeps it loading) must not match every offer
    w = watch_from_dict({"name": "x", "id": "x", "query": "пылесос дайсон", "max_price": 3000}, origin="telegram",
                        validate=False)
    for title in ("Cafea boabe Lavazza 1kg", "Televizor Samsung 55", "Aspirator Dyson V15"):
        ok, why = title_matches(w, title)
        assert not ok and "латинских" in why


# ------------------------------------------------------------------ 12: ids of non-Latin names
def test_slug_keeps_ids_of_latin_names():
    assert slug("iPhone 16 Pro 256GB") == "iphone-16-pro-256gb"
    assert slug("PS <5> & co") == "ps-5-co"
    assert slug("Mașină de spălat 9 kg") == "masina-de-spalat-9kg"
    assert slug("RTX™ 5070") == "rtxtm-5070"           # id from before ™ became a separator
    assert slug("TV 55″") == "tv-55"
    assert slug('OLED 55"') == "oled-55inch"
    assert slug("x" * 80) == "x" * 60


def test_slug_tells_non_latin_names_apart():
    a, b = slug("Телевизор LG"), slug("Монитор LG")
    assert a != b and a.startswith("lg-") and b.startswith("lg-")
    assert slug("Конкретный товар") != slug("Подарок маме")
    assert slug("Конкретный товар").startswith("watch-")
    assert slug("Телевизор LG") == a == slug("телевизор  lg")          # stable, case/space-insensitive
    assert len(slug("Видеокарта " + "RTX 5070 " * 20)) <= 60


def test_load_config_numbers_duplicate_auto_ids(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("""watches:
  - name: Телевизор LG
    query: televizor lg oled
  - name: Монитор LG
    query: monitor lg 27
  - name: Dyson
    query: aspirator dyson
  - name: dyson
    query: dyson v15
  - name: tv
    id: dyson-2
    query: tv
""", encoding="utf-8")
    cfg = load_config(str(p))
    ids = [w.id for w in cfg.watches]
    assert len(set(ids)) == len(ids)
    assert ids[2:] == ["dyson", "dyson-3", "dyson-2"]                   # an explicit id is never renamed
    assert load_config(str(p)).watches[0].id == ids[0]                  # same id on every run


def test_load_config_explicit_duplicate_ids_still_fail(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("watches:\n  - {name: a, id: same, query: a}\n  - {name: b, id: same, query: b}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="same"):
        load_config(str(p))


def test_load_config_reports_which_watch_is_wrong(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("watches:\n  - {name: ok, query: rtx 5070}\n  - {name: Пылесос, query: пылесос}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="пункт 2.*латиницей"):
        load_config(str(p))


# ------------------------------------------------------------------ 15: links
@pytest.mark.parametrize("url", ["https://[www.emag.ro/telefon/pd/D123", "www.emag.ro/x", "ftp://emag.ro/x",
                                 "https:///x", "https://emag.ro:abc/x", "javascript:alert(1)"])
def test_malformed_links_are_refused(url):
    with pytest.raises(ValueError, match="Некорректная ссылка"):
        watch_from_dict({"urls": [url], "max_price": 2000}, origin="telegram")


def test_good_links_and_stored_bad_links():
    w = watch_from_dict(parse_watch_spec("https://www.emag.ro/x/pd/ABC/ <2000"), origin="telegram")
    assert w.urls == ["https://www.emag.ro/x/pd/ABC/"] and w.max_price == 2000
    assert watch_from_dict({"urls": "http://shop.ro:8080/p/1"}).urls == ["http://shop.ro:8080/p/1"]
    # a watch saved by an older version still loads when re-read from state
    old = watch_from_dict({"name": "bad", "id": "bad", "urls": ["https://[www.emag.ro/x"]}, origin="telegram",
                          validate=False)
    assert old.id == "bad"


def test_string_numbers_in_config():
    assert watch_from_dict({"query": "x", "max_price": "4 800 lei"}).max_price == 4800
    assert watch_from_dict({"query": "x", "max_price": "5к"}).max_price == 5000
    with pytest.raises(ValueError):
        watch_from_dict({"query": "x", "max_price": "abc"})
    with pytest.raises(ValueError):
        watch_from_dict({"query": "   "})


# ------------------------------------------------------------------ 27: "X pentru <device>" accessories
@pytest.mark.parametrize("query,title", [
    ("aspirator dyson", "Filtru HEPA pentru aspirator Dyson V15 Detect"),
    ("aspirator dyson", "Perie motorizata pentru aspirator Dyson V8"),
    ("dyson v15", "Set 2 filtre pentru Dyson V15 Detect"),
    ("playstation 5", "Controller wireless Sony DualSense pentru PlayStation 5"),
    ("playstation 5", "Joc EA SPORTS FC 25 pentru PlayStation 5"),
    ("ps5 slim", "Statie de incarcare dubla pentru controller PS5 Slim"),
    ("iphone 16 pro", "Apple iPhone 16 Pro Clear Case with MagSafe"),
    ("iphone 16 pro", "Apple iPhone 16 Pro Silicone Case with MagSafe - Black"),
    ("frigider samsung", "Filtru de apa compatibil cu frigider Samsung RS68"),
    ("samsung galaxy s24", "Incarcator retea Samsung 25W pentru Galaxy S24"),
    ("iphone 16", "Husa silicon pentru iPhone 16"),
])
def test_accessories_for_the_searched_device_are_skipped(query, title):
    ok, why = title_matches(W(query=query), title)
    assert not ok and why.startswith("аксессуар")


@pytest.mark.parametrize("query,title", [
    ("aspirator dyson v15", "Aspirator vertical Dyson V15 Detect Absolute"),
    ("aspirator dyson v15", "Aspirator pentru casa Dyson V15 Detect"),
    ("dyson v15", "Aspirator pentru casa Dyson V15 Detect"),
    ("dior sauvage edp", "Parfum pentru barbati Dior Sauvage EDP 100 ml"),
    ("nike air max 90", "Adidasi pentru femei Nike Air Max 90"),
    ("bosch serie 6", "Cuptor pentru incastrare Bosch Serie 6 HBA5360B0"),
    ("royal canin maxi adult", "Hrana uscata pentru caini Royal Canin Maxi Adult 15kg"),
    ("playstation 5 slim", "Consola Sony PlayStation 5 Slim, pentru gaming 4K"),
    ("spider man 2", "Joc Marvel's Spider-Man 2 pentru PS5"),
    ("need for speed unbound", "Joc Need for Speed Unbound PS5"),
    ("logitech mx keys", "Tastatura Logitech MX Keys S for Mac, Space Grey"),
])
def test_real_products_with_pentru_or_for_are_kept(query, title):
    assert title_matches(W(query=query), title) == (True, "")


def test_query_with_pentru_disables_the_dependency_rule():
    assert tm("filtru pentru aspirator dyson", "Filtru HEPA pentru aspirator Dyson V15 Detect")


# ------------------------------------------------------------------ 28: category nouns that are real products
@pytest.mark.parametrize("query,title", [
    ("logitech mx keys", "Tastatura wireless Logitech MX Keys S, Bluetooth, Iluminare, Grafit"),
    ("lian li o11 dynamic", "Carcasa Lian Li O11 Dynamic EVO RGB, Mid-Tower, Negru"),
    ("xiaomi power bank 20000mah", "Baterie externa Xiaomi Redmi Power Bank 20000mAh, 18W"),
    ("bosch gsr 18v", "Bormasina cu acumulator Bosch Professional GSR 18V-55, 2 acumulatori"),
    ("the north face borealis", "Rucsac The North Face Borealis, 28L, Negru"),
    ("michael kors jet set", "Geanta Michael Kors Jet Set, piele, neagra"),
    ("anker 737", "Incarcator retea Anker 737 GaN, 120W"),
    ("huawei watch gt 5", "Ceas smartwatch Huawei Watch GT 5, 46mm, compatibil cu iPhone si Android"),
])
def test_category_nouns_are_products_when_searched_by_brand(query, title):
    assert title_matches(W(query=query), title) == (True, "")


def test_unambiguous_accessory_words_still_apply():
    w = W(query="iphone 16 pro 256gb", exclude=["max"])
    assert not title_matches(w, "Husa de protectie pentru iPhone 16 Pro 256GB")[0]
    assert not title_matches(w, "Folie sticla iPhone 16 Pro 256gb")[0]
    assert not tm("iphone 16", "Husa silicon iPhone 16, neagra")
    assert tm("husa iphone 16", "Husa silicon iPhone 16, neagra")
    assert not tm("galaxy s24", "Smartwatch compatibil cu iPhone, Galaxy S24 edition")   # no "android": accessory


# ------------------------------------------------------------------ 32: glued query words
@pytest.mark.parametrize("query,title", [
    ("rtx5070", "Placa video GIGABYTE GeForce RTX 5070 WINDFORCE OC SFF 12GB"),
    ("iphone16 pro", "Telefon mobil Apple iPhone 16 Pro, 256GB"),
    ("oled55c46la", "Televizor LG OLED evo 55C46LA, 139 cm, Smart, 4K"),
    ("rtx 5070", "Placa video RTX5070 WINDFORCE"),
    ("rtx 5070", "Placa video RTX-5070 WINDFORCE"),
    ("galaxy s 24", "Telefon Samsung Galaxy S24, 256GB"),
])
def test_glued_and_spaced_forms_match_each_other(query, title):
    assert title_matches(W(query=query), title) == (True, "")


def test_glued_split_keeps_number_rules():
    t = norm("Telefon mobil Apple iPhone 16 Pro, 256GB, 5G")
    assert has_term(t, "iphone16") and has_term(t, "256gb") and has_term(t, "256")
    assert not has_term(t, "iphone16e")
    assert not has_term(norm("iPhone 16e 128GB"), "iphone16")
    assert not has_term(norm("Placa video RTX 40700"), "rtx4070")
    assert not tm("rtx5070", "Placa video RTX 5070 Ti 16GB", exclude=["ti"])
    assert not tm("iphone16", "Apple iPhone 15, 16GB")             # glued parts apart need both words
    # "oled 55" pair: adjacent, glued or anywhere in the title, as before
    assert tm("televizor oled 55", 'Televizor Samsung 55" OLED')
    assert infer_categories("rtx5070") == {"pc"}
    assert "electronics" in infer_categories("iphone16 pro")
