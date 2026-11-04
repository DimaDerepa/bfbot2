"""Guess which store categories a query belongs to, so we don't ask fashion shops for GPUs.

Only used when the watch has no explicit `categories`/`stores` and settings.default_categories is empty.
Returns an empty set when unsure -> all stores are queried.
"""
from __future__ import annotations

from .text import has_term, norm, split_glued

KEYWORDS: dict[str, list[str]] = {
    "electronics": [
        "telefon", "smartphone", "iphone", "galaxy", "pixel", "xiaomi", "redmi", "oneplus", "motorola",
        "laptop", "notebook", "macbook", "ultrabook", "tableta", "ipad", "televizor", "tv", "oled", "qled",
        "monitor", "casti", "airpods", "headphones", "boxa", "soundbar", "consola", "playstation", "ps5", "ps4",
        "xbox", "nintendo", "switch 2", "smartwatch", "apple watch", "camera", "aparat foto", "gopro", "drona",
        "router", "imprimanta", "kindle", "ebook", "proiector", "videoproiector", "garmin", "fitbit",
    ],
    "pc": [
        "placa video", "rtx", "gtx", "radeon", "geforce", "procesor", "ryzen", "intel core", "core ultra", "ssd",
        "nvme", "hdd", "memorie ram", "ddr4", "ddr5", "placa de baza", "sursa", "cooler", "tastatura", "mouse",
        "monitor", "pc gaming", "sistem pc", "carcasa pc", "router", "webcam", "laptop",
    ],
    "appliances": [
        "frigider", "combina frigorifica", "congelator", "masina de spalat", "uscator de rufe", "cuptor", "plita",
        "hota", "aragaz", "aer conditionat", "microunde", "espressor", "expresor", "cafetiera", "aspirator",
        "robot de bucatarie", "fierbator", "prajitor", "airfryer", "air fryer", "friteuza", "mixer", "blender",
        "storcator", "fier de calcat", "statie de calcat", "centrala", "boiler", "purificator", "dezumidificator",
        "masina de spalat vase", "dyson", "roborock", "multicooker",
    ],
    "home": [
        "canapea", "saltea", "pat", "scaun", "birou", "lampa", "covor", "lenjerie", "oala", "tigaie", "cutite",
        "vesela", "perna", "pilota", "dulap", "mobilier", "aspirator", "espressor", "decoratiuni", "set oale",
    ],
    "diy": [
        "bormasina", "rotopercutor", "flex", "polizor", "surubelnita", "autofiletanta", "scule", "unelte",
        "motocoasa", "drujba", "generator", "compresor", "aparat de sudura", "masina de tuns", "nivela", "fierastrau",
    ],
    "toys": ["lego", "jucarie", "jucarii", "papusa", "barbie", "hot wheels", "playmobil", "puzzle", "trotineta copii"],
    "fashion": [
        "pantofi", "adidasi", "sneakers", "ghete", "cizme", "sandale", "geaca", "jacheta", "palton", "rochie",
        "blugi", "jeans", "tricou", "camasa", "hanorac", "pulover", "trening", "nike", "adidas", "puma",
        "new balance", "converse", "vans", "the north face", "columbia", "levis", "tommy hilfiger", "calvin klein",
        "geanta", "rucsac", "portofel", "ochelari de soare", "ceas", "bocanci", "colanti", "costum", "sacou",
    ],
    "beauty": [
        "parfum", "apa de parfum", "eau de parfum", "eau de toilette", "edp", "edt", "crema", "ser", "serum",
        "ruj", "fond de ten", "mascara", "sampon", "balsam de par", "lotiune", "deodorant", "machiaj", "fard",
        "epilator", "periuta electrica", "uscator de par", "placa de par", "airwrap", "skincare", "spf",
    ],
    "sport": ["bicicleta", "trotineta electrica", "banda de alergat", "gantere", "cort", "sac de dormit", "fitness"],
}


def infer_categories(query: str) -> set[str]:
    q = norm(query or "")
    if not q:
        return set()
    q = split_glued(q)          # "rtx5070" -> "rtx 5070", "iphone16" -> "iphone 16"
    found = {cat for cat, words in KEYWORDS.items() if any(has_term(q, w) for w in words)}
    return found
