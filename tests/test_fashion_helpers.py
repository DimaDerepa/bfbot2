import pytest

from bot.extract import make_soup
from bot.net import BlockedError
from bot.stores._fashion import (
    best_old, clean_url, current_price, is_old_el, join_title, labeled_prices, landed_on_home, money_values, num,
    old_price_in, price_lines, raise_if_challenge, stock_from_text,
)


def test_join_title_brand_and_name():
    assert join_title("adidas Originals", "Pantofi Samba") == "adidas Originals Pantofi Samba"
    assert join_title("Nike", "Nike Air Max 90 - Pantofi sport") == "Nike Air Max 90 - Pantofi sport"
    assert join_title("Tommy Jeans", "Tommy Jeans tricou") == "Tommy Jeans tricou"
    assert join_title("On", "Pantofi Cloudmonster") == "On Pantofi Cloudmonster"       # word-wise, not substring
    assert join_title("Lancôme", "Lancome Idôle") == "Lancome Idôle"                  # diacritics-insensitive
    assert join_title("", None, "  Rochie  midi ") == "Rochie midi"


def test_num_and_money():
    assert num("269.99") == 269.99 and num(1299) == 1299.0 and num("1.299,99 lei") == 1299.99
    assert num("") is None and num(True) is None and num("0") is None
    assert money_values("PRP: 1.599,99 Lei  1.299,99 lei 12,50 lei / 1 ml") == [1599.99, 1299.99, 12.5]


def test_price_lines_and_best_old():
    info = price_lines(["De la 449,99 lei", "Preț inițial: 549,99 lei", "Cel mai mic preț din ultimele 30 de zile: 499,99 lei"])
    assert info == {"price": 449.99, "original": 549.99, "lowest": 499.99, "price_from": True}
    assert best_old(449.99, 549.99, 499.99) == 499.99
    assert best_old(449.99, 549.99, 449.99) == 549.99              # 30-day low not above the price -> RRP
    assert best_old(449.99, None, None) is None
    unl = price_lines([["1.149,00 lei"], ["1.349,00 lei"], ["1.199,00 lei"]])
    assert unl["price"] == 1149.0 and unl["original"] == 1349.0 and unl["lowest"] == 1199.0
    assert price_lines(["12,50 lei / 100 ml", "89 lei"])["price"] == 89.0
    assert price_lines(["458,00 Lei* / 100 ml", "229,00 Lei*"])["price"] == 229.0
    assert labeled_prices(["Cel mai mic preț din ultimele 30 de zile: 1.349,00 lei", "1.299,90 lei"]) == (None, 1349.0)


def test_current_and_old_price_elements():
    soup = make_soup('<div class="box"><span class="product-price with-list-price">549,00 Lei*'
                     '<span class="list-price">699,00 Lei*</span></span>'
                     '<span class="unit">(1.098,00 Lei* / 100 ml)</span><span class="badge">-21%</span></div>')
    price_el = soup.select_one(".product-price")
    assert not is_old_el(price_el) and is_old_el(soup.select_one(".list-price"))
    assert current_price(price_el) == 549.0
    assert old_price_in(soup.select_one(".box"), 549.0, exclude=[price_el]) == 699.0
    assert current_price(make_soup("<p>13,58 lei / 1 ml</p>").p) is None
    assert current_price(make_soup("<p>sau 125 lei / luna</p>").p) is None
    assert is_old_el(make_soup('<span data-testid="original-price">1 lei</span>').span)
    assert not is_old_el(make_soup('<span class="fw-bold">1 lei</span>').span)


def test_clean_url_and_home_redirect():
    assert clean_url("/p/x/_/R-p-1?mc=88&utm_source=a#top", "https://www.decathlon.ro", keep=("mc",)) == \
        "https://www.decathlon.ro/p/x/_/R-p-1?mc=88"
    assert clean_url("javascript:void(0)", "https://x.ro") == ""
    assert landed_on_home("https://www.fashiondays.ro/search/?q=x", "https://www.fashiondays.ro/")
    assert landed_on_home("https://www.sinsay.com/ro/ro/?query=x", "https://www.sinsay.com/ro/ro/")
    assert not landed_on_home("https://www.sinsay.com/ro/ro/?query=x", "https://www.sinsay.com/ro/ro/?query=x")
    assert not landed_on_home("https://www.fashiondays.ro/search/?q=x", "https://www.fashiondays.ro/femei/pantofi/")


def test_stock_text():
    assert stock_from_text("Stoc epuizat") is False and stock_from_text("Momentan indisponibil") is False
    assert stock_from_text("În stoc") is True and stock_from_text("Ultimele 2 bucăți") is True
    assert stock_from_text("Livrare în 2 zile") is None


def test_raise_if_challenge_only_on_block_pages():
    normal = "<html><head><title>Rezultate pentru rochie</title></head><body>Nu am gasit produse."\
             "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script></body></html>"
    raise_if_challenge(normal, make_soup(normal), "https://x.ro/")               # passive CF script is not a block
    for page in ("<title>Just a moment...</title>", "<script>window._cf_chl_opt={}</script>",
                 "<title>Pardon Our Interruption</title>", "<script src='https://geo.captcha-delivery.com/x'></script>"):
        with pytest.raises(BlockedError):
            raise_if_challenge(page, make_soup(page), "https://x.ro/")


def test_num_thousands_dot_is_not_a_decimal():
    # visible Romanian text "1.299" is 1299 lei (research: '.' + exactly 3 digits = thousands separator)
    assert num("1.299") == 1299.0 and num("12.999") == 12999.0 and num("1.299.000") == 1299000.0
    assert num("269.99") == 269.99 and num("1299.990") == 1299.99 and num("449.5") == 449.5


def test_price_label_on_its_own_line_or_element():
    info = price_lines(["Prețul inițial", "549,99 lei", "449,99 lei"])
    assert info["price"] == 449.99 and info["original"] == 549.99
    info = price_lines(["449,99 lei", "Cel mai mic preț din ultimele 30 de zile:", "470,00 lei", "Inițial:", "549,99 lei"])
    assert (info["price"], info["original"], info["lowest"]) == (449.99, 549.99, 470.0)
    # a brand containing "Original(s)" is not a price label
    assert price_lines(["adidas Originals", "449,99 lei", "549,99 lei"])["price"] == 449.99
    assert labeled_prices(["Cel mai mic preț din ultimele 30 de zile:", "1.349 lei"]) == (None, 1349.0)
    assert labeled_prices(["adidas Originals", "99 lei"]) == (None, None)


def test_landed_on_home_locale_roots_only():
    assert landed_on_home("https://www.douglas.ro/ro/search?q=x", "https://www.douglas.ro/ro/")
    assert landed_on_home("https://www.douglas.ro/ro/search?q=x", "https://www.douglas.ro/")
    # a two-letter brand page is a listing, not the home page (Zalando /catalog/?q=hm -> /hm/)
    assert not landed_on_home("https://www.zalando.ro/catalog/?q=hm", "https://www.zalando.ro/hm/")
    assert not landed_on_home("https://www.zalando.ro/catalog/?q=x", "https://www.zalando.ro/catalog/?q=x")


def test_almost_sold_out_is_in_stock():
    assert stock_from_text("Stoc aproape epuizat") is True and stock_from_text("Produs aproape epuizat!") is True
    assert stock_from_text("Epuizat") is False and stock_from_text("Stoc epuizat") is False
