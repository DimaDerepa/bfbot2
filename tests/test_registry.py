import os

from bot.config import load_config
from bot.stores import IMPORT_ERRORS, registry, store_for_url

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATEGORIES = {"electronics", "pc", "home", "appliances", "fashion", "beauty", "general", "diy", "toys", "books", "sport"}


def test_config_yaml_loads():
    cfg = load_config(os.path.join(ROOT, "config.yaml"))
    assert cfg.settings.notify_drop_pct > 0


def test_all_store_modules_import():
    registry()
    assert IMPORT_ERRORS == {}


def test_store_attributes():
    reg = registry()
    assert len(reg) >= 2
    for key, cls in reg.items():
        assert cls.key == key and key == key.lower() and " " not in key, key
        assert cls.name, key
        assert cls.base_url.startswith("https://"), key
        assert cls.categories and set(cls.categories) <= CATEGORIES, (key, cls.categories)
        assert callable(getattr(cls, "search_page", None)), key


def test_store_for_url():
    reg = registry()
    assert store_for_url("https://altex.ro/x/cpd/ABC/", reg).key == "altex"
    assert store_for_url("https://www.unknown-shop.ro/p/1", reg) is None


def test_custom_shop_from_config():
    reg = registry([{"key": "mywoo", "name": "My Woo", "platform": "woocommerce", "base_url": "https://mywoo.ro"}])
    assert reg["mywoo"].name == "My Woo" and reg["mywoo"].base_url == "https://mywoo.ro"


# ---------------------------------------------------------------- finding 26: malformed settings.shops entries

def test_shop_entry_that_is_not_a_mapping_is_skipped():
    from bot.stores import SHOP_ERRORS

    reg = registry(["https://www.myshop.ro", None, 42,
                    {"key": "mywoo", "platform": "woocommerce", "base_url": "https://mywoo.ro"}])
    assert "altex" in reg and reg["mywoo"].base_url == "https://mywoo.ro"
    assert len(SHOP_ERRORS) == 3 and "myshop.ro" in SHOP_ERRORS["shops[0]"]


def test_shops_given_as_a_mapping():
    one = registry({"key": "mywoo", "platform": "woocommerce", "base_url": "https://mywoo.ro"})   # "- " forgotten
    assert one["mywoo"].name == "mywoo"
    many = registry({"shopa": {"platform": "shopify", "base_url": "https://a.ro"},
                     "shopb": {"platform": "woocommerce", "base_url": "https://b.ro", "name": "B"}})
    assert many["shopa"].base_url == "https://a.ro" and many["shopb"].name == "B"
    assert "altex" in registry("not a list") and "altex" in registry(None)


def test_any_exception_in_make_custom_store_is_logged_and_skipped(monkeypatch, caplog):
    import bot.stores as stores

    real = stores.make_custom_store

    def flaky(spec):
        if spec.get("key") == "bad":
            raise TypeError("boom")
        return real(spec)

    monkeypatch.setattr(stores, "make_custom_store", flaky)
    with caplog.at_level("ERROR", logger="bot.stores"):
        reg = registry([{"key": "bad", "base_url": "https://bad.ro"},
                        {"key": "nope", "platform": "bogus", "base_url": "https://nope.ro"},
                        {"key": "nourl", "platform": "woocommerce"},
                        {"key": "good", "platform": "woocommerce", "base_url": "https://good.ro"}])
    assert "good" in reg and not {"bad", "nope", "nourl"} & set(reg)
    assert "boom" in caplog.text and "bogus" in caplog.text
    assert set(stores.SHOP_ERRORS) == {"bad", "nope", "nourl"}


def test_custom_shop_categories_string_is_split():
    reg = registry([{"key": "a", "platform": "woocommerce", "base_url": "https://a.ro", "categories": "electronics"},
                    {"key": "b", "platform": "woocommerce", "base_url": "https://b.ro", "categories": "Electronics, pc"},
                    {"key": "c", "platform": "woocommerce", "base_url": "https://c.ro", "categories": ["home"]},
                    {"key": "d", "platform": "woocommerce", "base_url": "https://d.ro", "categories": None}])
    assert reg["a"].categories == ("electronics",)
    assert reg["b"].categories == ("electronics", "pc")
    assert reg["c"].categories == ("home",) and reg["d"].categories == ("general",)


def test_custom_shop_field_normalisation():
    reg = registry([
        {"key": "noscheme", "platform": "woocommerce", "base_url": "www.noscheme.ro/", "name": 5, "enabled": "false"},
        {"key": "rel", "platform": "html", "base_url": "https://rel.ro", "search_url": "/cautare?q={q}"},
        {"platform": None, "search_url": "https://www.only-search.ro/s?q={q}"},
    ])
    s = reg["noscheme"]
    assert s.base_url == "https://www.noscheme.ro" and s.domains == ("noscheme.ro",) and s.name == "5"
    assert s.enabled_by_default is False
    assert reg["rel"].search_url == "https://rel.ro/cautare?q={q}"
    assert reg["only-search"].base_url == "https://www.only-search.ro"
    assert store_for_url("https://noscheme.ro/p/1", reg).key == "noscheme"
