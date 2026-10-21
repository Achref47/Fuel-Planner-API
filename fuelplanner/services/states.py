"""U.S. state names and postal abbreviations (50 states + DC)."""

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}  # fmt: skip

_BY_NAME = {name.lower(): code for code, name in STATE_NAMES.items()}


def state_code(text: str) -> str | None:
    """Return the 2-letter code for "TX", "tx", "Texas" or "district of columbia"."""
    cleaned = " ".join(text.replace(".", "").split()).lower()
    if cleaned.upper() in STATE_NAMES:
        return cleaned.upper()
    return _BY_NAME.get(cleaned)


def split_trailing_state(text: str) -> tuple[str, str] | None:
    """Split "Fort Worth Texas" -> ("Fort Worth", "TX"); None if no state suffix."""
    words = text.split()
    for take in (3, 2, 1):  # "District of Columbia", "North Carolina", "TX"
        if len(words) > take:
            code = state_code(" ".join(words[-take:]))
            if code:
                return " ".join(words[:-take]), code
    return None
