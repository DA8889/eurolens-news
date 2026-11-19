"""Country codes for pair tagging: the 28 EuroLens countries, canonical codes and pair keys.

Mirrors eurolens/scripts/country_codes.py and pair_key.py (themselves mirrors of the app's
src/utils/countryCodes.js and pairKey.js). Codes are ISO 3166-1 alpha-2, except Greece = EL
(Eurostat convention); the UK is GB. Convert incoming codes with to_canonical() and build pair
keys with pair_key(), nowhere else.
"""

COUNTRIES = [
    ("AT", "Austria"), ("BE", "Belgium"), ("BG", "Bulgaria"), ("CY", "Cyprus"),
    ("CZ", "Czechia"), ("DE", "Germany"), ("DK", "Denmark"), ("EE", "Estonia"),
    ("EL", "Greece"), ("ES", "Spain"), ("FI", "Finland"), ("FR", "France"),
    ("GB", "United Kingdom"), ("HR", "Croatia"), ("HU", "Hungary"), ("IE", "Ireland"),
    ("IT", "Italy"), ("LT", "Lithuania"), ("LU", "Luxembourg"), ("LV", "Latvia"),
    ("MT", "Malta"), ("NL", "Netherlands"), ("PL", "Poland"), ("PT", "Portugal"),
    ("RO", "Romania"), ("SE", "Sweden"), ("SI", "Slovenia"), ("SK", "Slovakia"),
]
ALIASES = {"UK": "GB", "GR": "EL"}
NAMES = dict(COUNTRIES)
# GDELT's CAMEO actor country codes (3 letters) for the 28. Older CAMEO writes Romania ROM;
# verify_sources.py --gdelt counts which spelling the live files use.
CAMEO = {"AUT": "AT", "BEL": "BE", "BGR": "BG", "CYP": "CY", "CZE": "CZ", "DEU": "DE", "DNK": "DK",
         "EST": "EE", "GRC": "EL", "ESP": "ES", "FIN": "FI", "FRA": "FR", "GBR": "GB", "HRV": "HR",
         "HUN": "HU", "IRL": "IE", "ITA": "IT", "LTU": "LT", "LUX": "LU", "LVA": "LV", "MLT": "MT",
         "NLD": "NL", "POL": "PL", "PRT": "PT", "ROM": "RO", "ROU": "RO", "SWE": "SE", "SVN": "SI",
         "SVK": "SK"}


def to_canonical(code):
    """The canonical code, or None if the code is not one of the 28.

    >>> to_canonical("uk"), to_canonical("GR"), to_canonical(" de "), to_canonical("US")
    ('GB', 'EL', 'DE', None)
    >>> to_canonical(None)
    """
    if not code:
        return None
    upper = str(code).strip().upper()
    upper = ALIASES.get(upper, upper)
    return upper if upper in NAMES else None


def pair_key(code_a, code_b):
    """Alphabetical pair key: the same pair is never stored twice.

    >>> pair_key("DE", "AT"), pair_key("gb", "ie"), pair_key("GB", "AT")
    ('AT-DE', 'GB-IE', 'AT-GB')
    """
    return "-".join(sorted([code_a.strip().upper(), code_b.strip().upper()]))


def parse_pair(text):
    """A pair key from free text such as "FR-DE", "fr/de" or "UK-GR", or None if it is not two
    different countries among the 28.

    >>> parse_pair("FR-DE"), parse_pair("uk-gr"), parse_pair("DE-DE"), parse_pair("DE-US")
    ('DE-FR', 'EL-GB', None, None)
    >>> parse_pair("DE"), parse_pair(None)
    (None, None)
    """
    if not isinstance(text, str):
        return None
    parts = [to_canonical(p) for p in text.replace("/", "-").replace("_", "-").split("-")]
    if len(parts) != 2 or None in parts or parts[0] == parts[1]:
        return None
    return pair_key(*parts)


def parse_group(text):
    """The sorted, de-duplicated codes of free text such as "DK-AT-de" (2 or more countries
    acting together), or None unless every code is one of the 28 and at least two differ.

    >>> parse_group("DK-AT-de-NL"), parse_group("FR-DE"), parse_group("DE-DE"), parse_group("DE-FR-US")
    (['AT', 'DE', 'DK', 'NL'], ['DE', 'FR'], None, None)
    """
    if not isinstance(text, str):
        return None
    parts = [to_canonical(p) for p in text.replace("/", "-").replace("_", "-").split("-")]
    if None in parts or len(set(parts)) < 2:
        return None
    return sorted(set(parts))


def group_pairs(codes):
    """Every pair key among a group's codes.

    >>> group_pairs(["DE", "AT", "DK"]), group_pairs(["FR", "DE"])
    (['AT-DE', 'AT-DK', 'DE-DK'], ['DE-FR'])
    """
    codes = sorted(set(codes))
    return [pair_key(a, b) for i, a in enumerate(codes) for b in codes[i + 1:]]
