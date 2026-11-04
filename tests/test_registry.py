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
