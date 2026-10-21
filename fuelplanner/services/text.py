"""Place-name normalisation shared by the gazetteer build, the fuel import and lookups."""

import re
import unicodedata

# Census place names carry a type suffix ("Abbeville city", "Abanda CDP").
_LSAD_SUFFIX = re.compile(
    r"\s+(city and borough|consolidated government|metropolitan government|"
    r"unified government|metro government|urban county|charter township|city|town|"
    r"village|borough|cdp|municipality|township|plantation|gore|grant|location|"
    r"purchase|corporation)$",
    re.IGNORECASE,
)
_BALANCE = re.compile(r"\s*\(balance\)$", re.IGNORECASE)

# Common spelling variants of the same place word.
_VARIANTS = (
    (re.compile(r"\b(saint|st\.)(?=\s|$)"), "st"),
    (re.compile(r"\b(sainte|ste\.)(?=\s|$)"), "ste"),
    (re.compile(r"\b(fort|ft\.)(?=\s|$)"), "ft"),
    (re.compile(r"\b(mount|mt\.)(?=\s|$)"), "mt"),
)


def normalize_name(name: str) -> str:
    """Lower-case, ASCII-fold and squash a place name to ``[a-z0-9]`` only.

    Spaces and punctuation are dropped so "Mc Graw" == "McGraw" and
    "St. Louis" == "Saint Louis" == "St Louis".
    """
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    text = re.sub(r"\(.*?\)", " ", text.lower().strip())
    for pattern, replacement in _VARIANTS:
        text = pattern.sub(replacement, text)
    return re.sub(r"[^a-z0-9]", "", text)


def strip_place_suffix(name: str) -> str:
    """Remove the Census type suffix: "Abbeville city" -> "Abbeville"."""
    return _LSAD_SUFFIX.sub("", _BALANCE.sub("", name.strip()))


def place_key(state: str, city: str) -> str:
    """Lookup key for a (state, city) pair, e.g. ``"TX|stlouis"``."""
    return f"{state.strip().upper()}|{normalize_name(city)}"
