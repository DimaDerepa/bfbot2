"""Text normalisation and term matching for product titles (Romanian-aware)."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_DIACRITICS = str.maketrans({
    "ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t",
    "Ă": "a", "Â": "a", "Î": "i", "Ș": "s", "Ş": "s", "Ț": "t", "Ţ": "t",
})

# Units that may be glued to a number ("256 GB" -> "256gb", '55"' -> "55inch").
UNITS = (
    "gb", "tb", "mb", "hz", "khz", "mhz", "ghz", "mah", "wh", "w", "kw",
    "inch", "inchi", "cm", "mm", "ml", "l", "kg", "g", "mp", "btu", "rpm",
)
_UNIT_ALT = "|".join(sorted(UNITS, key=len, reverse=True))
_JOIN_UNIT_RE = re.compile(rf"(\d)\s+({_UNIT_ALT})\b")
_INCH_RE = re.compile(r"(\d)\s*(?:\"|''|”|″)")
_NON_WORD_RE = re.compile(r"[^0-9a-z.+/\-\s]")
_SPACES_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[0-9a-z]+(?:[.+][0-9a-z]+)*")

STOPWORDS = {
    "de", "si", "cu", "pentru", "la", "din", "pe", "in", "un", "o",
    "the", "for", "with", "and", "of", "a", "an",
    "и", "для", "с", "в", "на",
}


@lru_cache(maxsize=20000)
def norm(text: str) -> str:
    """Lowercase, strip diacritics/punctuation, glue numbers to units."""
    if not text:
        return ""
    s = text.translate(_DIACRITICS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _INCH_RE.sub(r"\1inch ", s)
    s = s.lower()
    s = _NON_WORD_RE.sub(" ", s)
    # "/" and "-" separate words unless between digits/letters of a model code ("rtx-4070" stays split, fine)
    s = s.replace("/", " ").replace(" - ", " ")
    s = _SPACES_RE.sub(" ", s).strip()
    s = _JOIN_UNIT_RE.sub(r"\1\2", s)
    return s


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(norm(text).replace("-", " "))


def query_terms(query: str) -> list[str]:
    """Significant tokens of a search query (used for default strict matching)."""
    return [t for t in tokens(query) if t not in STOPWORDS]


@lru_cache(maxsize=5000)
def _term_regex(term: str) -> re.Pattern | None:
    t = term.strip()
    if not t:
        return None
    if t.lower().startswith("re:"):
        try:
            return re.compile(t[3:], re.IGNORECASE)
        except re.error:
            return None
    nt = norm(t).replace("-", " ")
    nt = _SPACES_RE.sub(" ", nt).strip()
    if not nt:
        return None
    # words inside the term may be separated by space or hyphen in the title
    body = r"[\s\-]?".join(re.escape(w) for w in nt.split(" "))
    left = r"(?<![0-9a-z])"
    if nt[-1].isdigit():
        # "256" matches "256gb" (unit glued) but not "2560"; "16" does not match "16e"
        right = rf"(?:(?![0-9a-z])|(?=(?:{_UNIT_ALT})(?![0-9a-z])))"
    else:
        right = r"(?![0-9a-z])"
    return re.compile(left + body + right)


def has_term(title_norm: str, term: str) -> bool:
    """True if `term` occurs in the already-normalised title with word boundaries."""
    rx = _term_regex(term)
    if rx is None:
        return False
    target = title_norm.replace("-", " ") if not term.lower().startswith("re:") else title_norm
    return bool(rx.search(target))


def slug(text: str) -> str:
    s = norm(text)
    s = re.sub(r"[^0-9a-z]+", "-", s).strip("-")
    return s[:60] or "watch"
