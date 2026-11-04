import os

from bot.stores.kaufland import OFFERS_URL, KauflandStore
from tests.conftest import FakeHttp, make_store

HTML = open(os.path.join(os.path.dirname(__file__), "fixtures", "kaufland_offers.html"), encoding="utf-8").read()


def test_kaufland_flyer_filtered_by_query():
    http = FakeHttp({"kaufland.ro/oferte/prezentare-generala-oferte.html": HTML})
    store = make_store(KauflandStore, http)
    tv = store.search("televizor samsung")
    assert http.calls == ["https://www.kaufland.ro/oferte/prezentare-generala-oferte.html?kloffer-week=current"]
    assert len(tv) == 1                                   # same tile in two sections -> one offer
    o = tv[0]
    assert o.title == "Samsung Televizor Smart LED UE55DU7172, 138 cm, 4K"
    assert o.price == 1799.99 and o.old_price == 2399.99 and o.in_stock is None and o.seller is None
    assert o.url == "https://www.kaufland.ro/oferte/oferte-saptamanale/saptamana-curenta.category=08_Electro.html#offer_5011"
    assert o.offer_id and o.offer_id.startswith("samsung-televizor-smart-led")
    assert o.image.endswith("Asset_10155011.jpg")

    milk = store.search("lapte zuzu")
    assert len(http.calls) == 1                           # flyer fetched once per run
    assert sorted(m.price for m in milk) == [7.99, 8.99]
    card = next(m for m in milk if m.price == 7.99)
    assert card.seller == "Kaufland Card" and card.old_price == 9.99 and card.url == OFFERS_URL
    regular = next(m for m in milk if m.price == 8.99)
    assert regular.offer_id != card.offer_id and regular.old_price is None

    fryer = store.search("friteuza")                      # diacritics-insensitive local match
    assert [(f.offer_id, f.price, f.old_price) for f in fryer] == [("10159901", 299.0, 399.0)]
    assert store.search("aspirator philips") == []        # tile without a price ("2+1 gratis") is skipped
    assert store.search("iphone") == []
    assert store.search_page("televizor", 2) == []

    # tile wrapped in an <a>, with a regular price tag and a Kaufland Card one: one offer per tag
    butter = {o.price: o for o in store.search("unt pilos")}
    assert set(butter) == {9.49, 6.99}
    assert butter[9.49].seller is None and butter[9.49].old_price is None
    assert butter[6.99].seller == "Kaufland Card" and butter[6.99].old_price == 9.49
    assert butter[6.99].offer_id != butter[9.49].offer_id and butter[6.99].offer_id.endswith("card")
    assert butter[9.49].url == ("https://www.kaufland.ro/oferte/oferte-saptamanale/saptamana-curenta.html"
                                "#offer_7781")              # utm_* dropped, link taken from the wrapping <a>
    assert len(http.calls) == 1


def test_kaufland_challenge_page_with_http_200():
    import pytest
    from bot.net import BlockedError

    page = "<html><head><title>Access Denied</title></head><body>" + "<p>.</p>" * 9000 + "</body></html>"
    http = FakeHttp({"kaufland.ro": page})
    store = make_store(KauflandStore, http)
    with pytest.raises(BlockedError):
        store.search("lapte")
    with pytest.raises(BlockedError):
        store.search("unt")
    assert len(http.calls) == 1


def test_kaufland_failed_flyer_fetch_not_repeated():
    import pytest
    from bot.net import FetchError

    http = FakeHttp({"kaufland.ro": (500, "<html>Internal error</html>")})
    store = make_store(KauflandStore, http)
    for q in ("lapte", "unt", "paine"):
        with pytest.raises(FetchError):
            store.search(q)
    assert len(http.calls) == 1                           # the error is reported per query, the page fetched once
