import os

from bot.extract import make_soup
from bot.stores._itshops import (
    clean_url, current_price, el_price, is_challenge, landed_on_home, next_page_url, num_attr, old_price_in,
    stock_from,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _el(html):
    return make_soup(html).body.find(True)


def test_prices():
    assert num_attr("1798.99") == 1798.99            # machine value: float first (Romanian rule would give 179899)
    assert num_attr("1.798,99 RON") == 1798.99
    assert num_attr("0") is None and num_attr(None) is None and num_attr(True) is None
    assert current_price(_el('<span>2.199<sup>99</sup> Lei</span>')) == 2199.99
    assert current_price(_el('<span>2.199,<sup>99</sup> Lei</span>')) == 2199.99
    assert current_price(_el('<p>5.299<sup><small>,</small>99</sup> <span>Lei</span></p>')) == 5299.99
    assert current_price(_el('<div><del>2.499,99 lei</del> 1.999,99 lei</div>')) == 1999.99
    assert current_price(_el('<b itemprop="price" content="1798.990">1.798,99 lei</b>')) == 1798.99
    assert current_price(_el('<span class="price">1.016</span>')) == 1016.0
    # nested struck elements (regression: removing a parent first must not break its old children)
    assert current_price(_el('<p><del><s class="old">9 lei</s></del><span class="x-old">8 lei</span> 7 lei</p>')) == 7.0
    # instalment / saving lines and "-12%" badges inside a price box are not the price...
    assert current_price(_el('<div><span>-12%</span><div>sau 125 Lei / luna</div><span>Economisesti 300 lei</span>'
                             '<span>2.199,99 Lei</span></div>')) == 2199.99
    # ...but a wrapper holding the real price next to an instalment child is kept
    assert current_price(_el('<div><div class="w">1.499 lei <small>sau 63 lei/luna</small></div></div>')) == 1499.0


def test_old_price_tokens():
    card = _el('<div><span class="price">1.999 lei</span><span class="fw-bold">9.999 lei</span>'
               '<span class="price_initial">2.499 lei</span><s>2.999 lei</s>'
               '<span class="old-price">Rate 99 lei/luna</span><del>1.500 lei</del></div>')
    assert old_price_in(card, 1999.0, exclude=[card.select_one(".price")]) == 2999.0   # "fw-bold" is not "old"


def test_stock():
    assert stock_from(["product_box_availability", "instock"], "Stoc magazin limitat") is True
    assert stock_from(["availability", "insupplierstock"], "in stoc depozit/furnizor") is True
    assert stock_from(["product_box_availability", "outofstock"], "Stoc epuizat") is False
    assert stock_from(["info_stoc", "in_stoc"], "In stoc") is True
    assert stock_from(["info_stoc", "no_stoc"], "") is False
    assert stock_from([], "Nu este in stoc") is False
    assert stock_from([], "Produsul nu este disponibil") is False
    assert stock_from([], "Adauga in cos") is None


def test_challenge_detection():
    cf = open(os.path.join(FIX, "pcgarage_search.html"), encoding="utf-8").read()
    assert not is_challenge(cf, make_soup(cf))       # passive Bot Management loader only
    managed = "<html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt={}</script></body></html>"
    assert is_challenge(managed, make_soup(managed))
    turnstile = '<html><head><title>Newsletter</title></head><body><div class="cf-turnstile"></div></body></html>'
    assert not is_challenge(turnstile, make_soup(turnstile))
    robot = "<html><head><title>Robot de bucatarie - Cautare</title></head><body></body></html>"
    assert not is_challenge(robot, make_soup(robot))


def test_clean_url_and_pager():
    assert clean_url("/a/b-1.html?utm_source=x#top", "https://www.evomag.ro/x/") == "https://www.evomag.ro/a/b-1.html"
    assert clean_url("javascript:void(0)", "https://x.ro/") == ""
    soup = make_soup('<ul class="pagination"><li><span>1</span></li><li><a href="/c/?q=a&amp;p=2">2</a></li>'
                     '<li><a href="javascript:void(0)">&raquo;</a></li></ul>')
    assert next_page_url(soup, "https://x.ro/c/?q=a", 1) == "https://x.ro/c/?q=a&p=2"
    assert next_page_url(soup, "https://x.ro/c/?q=a&p=2", 2) is None
    # ">>" is "last page" on some pagers: the next page number wins over it
    last = make_soup('<div class="pageresults"><span>1</span><a class="last" href="/c/9a/">&gt;&gt;</a>'
                     '<a href="/c/2a/">2</a><a href="/c/3a/">3</a></div>')
    assert next_page_url(last, "https://x.ro/c/", 1) == "https://x.ro/c/2a/"
    arrows = make_soup('<ul class="pagination"><li><a href="/c/?page=2" aria-label="Next">&raquo;</a></li></ul>')
    assert next_page_url(arrows, "https://x.ro/c/", 1) == "https://x.ro/c/?page=2"


def test_landed_on_home():
    assert landed_on_home("https://www.cel.ro/cauta/ssd/", "https://www.cel.ro/")
    assert landed_on_home("https://www.evomag.ro/?sn.q=rtx", "https://www.evomag.ro/")
    assert not landed_on_home("https://www.evomag.ro/?sn.q=rtx", "https://www.evomag.ro/?sn.q=rtx")
    assert not landed_on_home("https://www.pcgarage.ro/cauta/?q=gpu", "https://www.pcgarage.ro/placi-video/")
    assert not landed_on_home("https://www.vexio.ro/", "https://www.vexio.ro/")        # home asked for


def test_el_price_thousands_dot_attribute():
    assert el_price(_el('<span class="price" content="1.849">1.849</span>')) == 1849.0
    assert el_price(_el('<span class="price" content="1849">1849</span>')) == 1849.0
    assert el_price(_el('<span content="18.49">18,49 lei</span>')) == 18.49
