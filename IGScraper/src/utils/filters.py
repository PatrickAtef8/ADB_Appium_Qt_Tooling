"""
Filter accounts based on configurable rules.
Supports: keywords, no-bio, private, no-contact, min posts, no profile pic, story detection.
"""
import re
from datetime import datetime, timedelta
from typing import List


# ── Contact extraction ────────────────────────────────────────────────────────

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+",
    re.IGNORECASE,
)

PHONE_RE = re.compile(
    r"(?:\+?[\d\s\-\(\)]{7,20})",
)

# ── NANP (+1) area code disambiguation ───────────────────────────────────────
# The NANP (North American Numbering Plan) shares +1 between the US, Canada,
# and many Caribbean nations.  We must inspect the 3-digit area code to
# tell them apart.  Any +1 NXX not listed below defaults to US.

# Canadian area codes (all confirmed active or reserved as of 2025)
_CA_AREA_CODES = {
    "204", "226", "236", "249", "250", "289", "306", "343", "365", "367",
    "368", "382", "387", "403", "416", "418", "428", "431", "437", "438",
    "450", "468", "474", "506", "514", "519", "548", "579", "581", "584",
    "587", "600", "604", "613", "621", "639", "647", "672", "683", "705",
    "709", "742", "753", "778", "780", "782", "807", "819", "825", "867",
    "873", "879", "902", "905",
}

# Caribbean / NANP island area codes -> ISO country code
_CARIBBEAN_AREA_CODES = {
    "242": "BS",  # Bahamas
    "246": "BB",  # Barbados
    "264": "AI",  # Anguilla
    "268": "AG",  # Antigua & Barbuda
    "284": "VG",  # British Virgin Islands
    "340": "VI",  # US Virgin Islands
    "345": "KY",  # Cayman Islands
    "441": "BM",  # Bermuda
    "473": "GD",  # Grenada
    "649": "TC",  # Turks & Caicos
    "664": "MS",  # Montserrat
    "671": "GU",  # Guam
    "684": "AS",  # American Samoa
    "721": "SX",  # Sint Maarten
    "758": "LC",  # St. Lucia
    "767": "DM",  # Dominica
    "784": "VC",  # St. Vincent & the Grenadines
    "787": "PR",  # Puerto Rico
    "809": "DO",  # Dominican Republic
    "829": "DO",  # Dominican Republic
    "849": "DO",  # Dominican Republic
    "868": "TT",  # Trinidad & Tobago
    "869": "KN",  # St. Kitts & Nevis
    "876": "JM",  # Jamaica
    "939": "PR",  # Puerto Rico
}


def _resolve_nanp(digits: str) -> str:
    """
    Given digits starting with '1' (NANP country code), return the correct
    ISO 2-letter country code by inspecting the 3-digit area code.
    digits must already be stripped of leading '+' or '00'.
    """
    if len(digits) < 4:
        return "US"
    area = digits[1:4]
    if area in _CA_AREA_CODES:
        return "CA"
    if area in _CARIBBEAN_AREA_CODES:
        return _CARIBBEAN_AREA_CODES[area]
    return "US"


# ── Country-code prefix table ─────────────────────────────────────────────────
# Checked longest-match first (3 digits -> 2 digits -> 1 digit).
# "1" (NANP) is intentionally absent — handled by _resolve_nanp() above.
COUNTRY_PHONE_PREFIXES = {
    # ── 3-digit prefixes ────────────────────────────────────────────────────
    # Middle East
    "961": "LB",  # Lebanon
    "962": "JO",  # Jordan
    "963": "SY",  # Syria
    "964": "IQ",  # Iraq
    "965": "KW",  # Kuwait
    "966": "SA",  # Saudi Arabia
    "967": "YE",  # Yemen
    "968": "OM",  # Oman
    "970": "PS",  # Palestine
    "971": "AE",  # UAE
    "972": "IL",  # Israel
    "973": "BH",  # Bahrain
    "974": "QA",  # Qatar
    "975": "BT",  # Bhutan
    "976": "MN",  # Mongolia
    "977": "NP",  # Nepal
    "960": "MV",  # Maldives
    # Africa
    "212": "MA",  # Morocco
    "213": "DZ",  # Algeria
    "216": "TN",  # Tunisia
    "218": "LY",  # Libya
    "220": "GM",  # Gambia
    "221": "SN",  # Senegal
    "222": "MR",  # Mauritania
    "223": "ML",  # Mali
    "224": "GN",  # Guinea
    "225": "CI",  # Côte d'Ivoire
    "226": "BF",  # Burkina Faso
    "227": "NE",  # Niger
    "228": "TG",  # Togo
    "229": "BJ",  # Benin
    "230": "MU",  # Mauritius
    "231": "LR",  # Liberia
    "232": "SL",  # Sierra Leone
    "233": "GH",  # Ghana
    "234": "NG",  # Nigeria
    "235": "TD",  # Chad
    "236": "CF",  # Central African Republic
    "237": "CM",  # Cameroon
    "238": "CV",  # Cape Verde
    "239": "ST",  # São Tomé & Príncipe
    "240": "GQ",  # Equatorial Guinea
    "241": "GA",  # Gabon
    "242": "CG",  # Republic of Congo
    "243": "CD",  # DR Congo
    "244": "AO",  # Angola
    "245": "GW",  # Guinea-Bissau
    "248": "SC",  # Seychelles
    "249": "SD",  # Sudan
    "250": "RW",  # Rwanda
    "251": "ET",  # Ethiopia
    "252": "SO",  # Somalia
    "253": "DJ",  # Djibouti
    "254": "KE",  # Kenya
    "255": "TZ",  # Tanzania
    "256": "UG",  # Uganda
    "257": "BI",  # Burundi
    "258": "MZ",  # Mozambique
    "260": "ZM",  # Zambia
    "261": "MG",  # Madagascar
    "262": "RE",  # Réunion
    "263": "ZW",  # Zimbabwe
    "264": "NA",  # Namibia
    "265": "MW",  # Malawi
    "266": "LS",  # Lesotho
    "267": "BW",  # Botswana
    "268": "SZ",  # Eswatini
    "269": "KM",  # Comoros
    "291": "ER",  # Eritrea
    # South/Southeast Asia
    "850": "KP",  # North Korea
    "853": "MO",  # Macau
    "855": "KH",  # Cambodia
    "856": "LA",  # Laos
    "880": "BD",  # Bangladesh
    "886": "TW",  # Taiwan
    # Central Asia / Caucasus
    "992": "TJ",  # Tajikistan
    "993": "TM",  # Turkmenistan
    "994": "AZ",  # Azerbaijan
    "995": "GE",  # Georgia
    "996": "KG",  # Kyrgyzstan
    "998": "UZ",  # Uzbekistan
    # Europe (3-digit)
    "350": "GI",  # Gibraltar
    "351": "PT",  # Portugal
    "352": "LU",  # Luxembourg
    "353": "IE",  # Ireland
    "354": "IS",  # Iceland
    "355": "AL",  # Albania
    "356": "MT",  # Malta
    "357": "CY",  # Cyprus
    "358": "FI",  # Finland
    "359": "BG",  # Bulgaria
    "370": "LT",  # Lithuania
    "371": "LV",  # Latvia
    "372": "EE",  # Estonia
    "373": "MD",  # Moldova
    "374": "AM",  # Armenia
    "375": "BY",  # Belarus
    "376": "AD",  # Andorra
    "377": "MC",  # Monaco
    "378": "SM",  # San Marino
    "380": "UA",  # Ukraine
    "381": "RS",  # Serbia
    "382": "ME",  # Montenegro
    "383": "XK",  # Kosovo
    "385": "HR",  # Croatia
    "386": "SI",  # Slovenia
    "387": "BA",  # Bosnia & Herzegovina
    "389": "MK",  # North Macedonia
    "420": "CZ",  # Czech Republic
    "421": "SK",  # Slovakia
    "423": "LI",  # Liechtenstein
    # Latin America (3-digit)
    "502": "GT",  # Guatemala
    "503": "SV",  # El Salvador
    "504": "HN",  # Honduras
    "505": "NI",  # Nicaragua
    "506": "CR",  # Costa Rica
    "507": "PA",  # Panama
    "509": "HT",  # Haiti
    "590": "GP",  # Guadeloupe
    "591": "BO",  # Bolivia
    "592": "GY",  # Guyana
    "593": "EC",  # Ecuador
    "594": "GF",  # French Guiana
    "595": "PY",  # Paraguay
    "596": "MQ",  # Martinique
    "597": "SR",  # Suriname
    "598": "UY",  # Uruguay
    "599": "CW",  # Curaçao
    # Pacific
    "670": "TL",  # Timor-Leste
    "673": "BN",  # Brunei
    "674": "NR",  # Nauru
    "675": "PG",  # Papua New Guinea
    "676": "TO",  # Tonga
    "677": "SB",  # Solomon Islands
    "678": "VU",  # Vanuatu
    "679": "FJ",  # Fiji
    "680": "PW",  # Palau
    "681": "WF",  # Wallis & Futuna
    "682": "CK",  # Cook Islands
    "683": "NU",  # Niue
    "685": "WS",  # Samoa
    "686": "KI",  # Kiribati
    "687": "NC",  # New Caledonia
    "688": "TV",  # Tuvalu
    "689": "PF",  # French Polynesia
    "691": "FM",  # Micronesia
    "692": "MH",  # Marshall Islands
    # ── 2-digit prefixes ────────────────────────────────────────────────────
    "20": "EG",   # Egypt
    "27": "ZA",   # South Africa
    "30": "GR",   # Greece
    "31": "NL",   # Netherlands
    "32": "BE",   # Belgium
    "33": "FR",   # France
    "34": "ES",   # Spain
    "36": "HU",   # Hungary
    "39": "IT",   # Italy
    "40": "RO",   # Romania
    "41": "CH",   # Switzerland
    "43": "AT",   # Austria
    "44": "GB",   # United Kingdom
    "45": "DK",   # Denmark
    "46": "SE",   # Sweden
    "47": "NO",   # Norway
    "48": "PL",   # Poland
    "49": "DE",   # Germany
    "51": "PE",   # Peru
    "52": "MX",   # Mexico
    "53": "CU",   # Cuba
    "54": "AR",   # Argentina
    "55": "BR",   # Brazil
    "56": "CL",   # Chile
    "57": "CO",   # Colombia
    "58": "VE",   # Venezuela
    "60": "MY",   # Malaysia
    "61": "AU",   # Australia
    "62": "ID",   # Indonesia
    "63": "PH",   # Philippines
    "64": "NZ",   # New Zealand
    "65": "SG",   # Singapore
    "66": "TH",   # Thailand
    "81": "JP",   # Japan
    "82": "KR",   # South Korea
    "84": "VN",   # Vietnam
    "86": "CN",   # China
    "90": "TR",   # Turkey
    "91": "IN",   # India
    "92": "PK",   # Pakistan
    "93": "AF",   # Afghanistan
    "94": "LK",   # Sri Lanka
    "95": "MM",   # Myanmar
    "98": "IR",   # Iran
    # ── 1-digit prefix ──────────────────────────────────────────────────────
    # NOTE: "1" (NANP) is NOT here — handled by _resolve_nanp() above
    "7": "RU",    # Russia (also covers Kazakhstan 76x/77x; defaults to RU)
}


def extract_email(text: str) -> str:
    """Extract first email address found in text."""
    if not text:
        return ""
    m = EMAIL_RE.search(text)
    return m.group(0) if m else ""


def extract_phone(text: str) -> str:
    """Extract first phone-like number from text."""
    if not text:
        return ""
    matches = PHONE_RE.findall(text)
    for m in matches:
        cleaned = re.sub(r"[\s\-\(\)]", "", m)
        if len(cleaned) >= 7:
            return cleaned
    return ""


def infer_country_code(phone: str, location_text: str = "") -> str:
    """
    Try to infer 2-letter country code from location text or phone prefix.
    Location text is checked FIRST - it is more reliable than phone prefixes
    (which can be ambiguous, e.g. local numbers without country code).
    Falls back to phone prefix only when location gives no result.

    Key fix: +1 numbers (NANP) are disambiguated by area code so that
    Canadian numbers (e.g. +1-647-...) are correctly identified as CA,
    Caribbean island numbers map to their specific country, and everything
    else defaults to US.
    """
    COUNTRY_KEYWORDS = {
        # North America
        "united states": "US", "usa": "US", "u.s.a": "US", "u.s.": "US",
        "canada": "CA",
        "mexico": "MX", "mexico": "MX",
        # Europe
        "united kingdom": "GB", "uk": "GB", "england": "GB",
        "scotland": "GB", "wales": "GB", "northern ireland": "GB",
        "germany": "DE", "deutschland": "DE",
        "france": "FR",
        "spain": "ES", "espana": "ES",
        "italy": "IT", "italia": "IT",
        "portugal": "PT",
        "netherlands": "NL", "holland": "NL",
        "belgium": "BE", "belgique": "BE",
        "switzerland": "CH", "suisse": "CH",
        "austria": "AT", "osterreich": "AT",
        "sweden": "SE", "sverige": "SE",
        "norway": "NO", "norge": "NO",
        "denmark": "DK", "danmark": "DK",
        "finland": "FI", "suomi": "FI",
        "poland": "PL", "polska": "PL",
        "czech": "CZ", "czechia": "CZ",
        "slovakia": "SK",
        "hungary": "HU",
        "romania": "RO",
        "bulgaria": "BG",
        "greece": "GR",
        "croatia": "HR",
        "serbia": "RS",
        "ukraine": "UA",
        "russia": "RU",
        "luxembourg": "LU",
        "malta": "MT",
        "cyprus": "CY",
        "iceland": "IS",
        "albania": "AL",
        "moldova": "MD",
        "belarus": "BY",
        "latvia": "LV",
        "lithuania": "LT",
        "estonia": "EE",
        "ireland": "IE",
        # Middle East
        "saudi": "SA", "saudi arabia": "SA", "ksa": "SA",
        "uae": "AE", "emirates": "AE", "dubai": "AE", "abu dhabi": "AE",
        "jordan": "JO",
        "kuwait": "KW",
        "qatar": "QA",
        "bahrain": "BH",
        "oman": "OM",
        "yemen": "YE",
        "iraq": "IQ",
        "iran": "IR",
        "syria": "SY",
        "lebanon": "LB",
        "israel": "IL",
        "palestine": "PS",
        # Africa
        "egypt": "EG",
        "morocco": "MA",
        "algeria": "DZ",
        "tunisia": "TN",
        "libya": "LY",
        "sudan": "SD",
        "nigeria": "NG",
        "kenya": "KE",
        "ghana": "GH",
        "south africa": "ZA",
        "ethiopia": "ET",
        "tanzania": "TZ",
        "uganda": "UG",
        "senegal": "SN",
        "cameroon": "CM",
        "angola": "AO",
        "mozambique": "MZ",
        "zimbabwe": "ZW",
        # Asia
        "india": "IN",
        "china": "CN",
        "japan": "JP",
        "south korea": "KR", "korea": "KR",
        "indonesia": "ID",
        "malaysia": "MY",
        "philippines": "PH",
        "vietnam": "VN",
        "thailand": "TH",
        "singapore": "SG",
        "pakistan": "PK",
        "bangladesh": "BD",
        "sri lanka": "LK",
        "nepal": "NP",
        "myanmar": "MM", "burma": "MM",
        "cambodia": "KH",
        "taiwan": "TW",
        "hong kong": "HK",
        "turkey": "TR", "turkiye": "TR",
        "afghanistan": "AF",
        "uzbekistan": "UZ",
        "kazakhstan": "KZ",
        "azerbaijan": "AZ",
        "georgia": "GE",
        "armenia": "AM",
        # Oceania
        "australia": "AU",
        "new zealand": "NZ",
        "fiji": "FJ",
        "papua new guinea": "PG",
        # Latin America
        "brazil": "BR", "brasil": "BR",
        "argentina": "AR",
        "chile": "CL",
        "colombia": "CO",
        "peru": "PE",
        "venezuela": "VE",
        "ecuador": "EC",
        "bolivia": "BO",
        "paraguay": "PY",
        "uruguay": "UY",
        "cuba": "CU",
        "haiti": "HT",
        "dominican republic": "DO",
        "puerto rico": "PR",
        "jamaica": "JM",
        "trinidad": "TT",
        "costa rica": "CR",
        "panama": "PA",
        "guatemala": "GT",
        "honduras": "HN",
        "el salvador": "SV",
        "nicaragua": "NI",
    }

    # ── 1. Location text FIRST (most reliable) ────────────────────────────────
    if location_text:
        loc_lower = location_text.lower()
        for keyword, code in COUNTRY_KEYWORDS.items():
            # Use word-boundary match to avoid false hits like
            # "iran" inside "Almirante" or "ir" inside "Madrid"
            if re.search(r"\b" + re.escape(keyword) + r"\b", loc_lower):
                return code

    # ── 2. Phone prefix fallback (only when location gave nothing) ────────────
    if phone:
        # Detect whether the number was in explicit international format BEFORE
        # stripping non-digits. Only +XX or 00XX prefixed numbers reliably carry
        # a country code — local numbers that happen to start with the same
        # digits as a country code (e.g. Spanish "912…" vs India "+91") must NOT
        # be matched against 1- or 2-digit country prefixes.
        phone_stripped = phone.strip()
        is_international = phone_stripped.startswith("+") or phone_stripped.startswith("00")

        cleaned = re.sub(r"[^\d]", "", phone_stripped)
        if cleaned.startswith("00"):
            cleaned = cleaned[2:]

        # NANP special case — only when international format confirmed
        if is_international and cleaned.startswith("1") and len(cleaned) >= 4:
            return _resolve_nanp(cleaned)

        # 3-digit prefixes are globally unambiguous — safe to match always.
        prefix3 = cleaned[:3]
        if prefix3 in COUNTRY_PHONE_PREFIXES:
            return COUNTRY_PHONE_PREFIXES[prefix3]

        # 2-digit and 1-digit prefixes are only safe when the number explicitly
        # carries a country code (i.e. international format).
        if is_international:
            for length in (2, 1):
                prefix = cleaned[:length]
                if prefix in COUNTRY_PHONE_PREFIXES:
                    return COUNTRY_PHONE_PREFIXES[prefix]

    return ""


# ── Country code → full country name ─────────────────────────────────────────

COUNTRY_CODE_TO_NAME: dict = {
    "AF": "Afghanistan", "AL": "Albania", "DZ": "Algeria", "AO": "Angola",
    "AR": "Argentina", "AM": "Armenia", "AU": "Australia", "AT": "Austria",
    "AZ": "Azerbaijan", "BH": "Bahrain", "BD": "Bangladesh", "BY": "Belarus",
    "BE": "Belgium", "BO": "Bolivia", "BR": "Brazil", "BG": "Bulgaria",
    "KH": "Cambodia", "CM": "Cameroon", "CA": "Canada", "CL": "Chile",
    "CN": "China", "CO": "Colombia", "CR": "Costa Rica", "HR": "Croatia",
    "CU": "Cuba", "CY": "Cyprus", "CZ": "Czech Republic", "DK": "Denmark",
    "DO": "Dominican Republic", "EC": "Ecuador", "EG": "Egypt",
    "SV": "El Salvador", "EE": "Estonia", "ET": "Ethiopia", "FJ": "Fiji",
    "FI": "Finland", "FR": "France", "GE": "Georgia", "DE": "Germany",
    "GH": "Ghana", "GR": "Greece", "GT": "Guatemala", "HT": "Haiti",
    "HN": "Honduras", "HK": "Hong Kong", "HU": "Hungary", "IS": "Iceland",
    "IN": "India", "ID": "Indonesia", "IR": "Iran", "IQ": "Iraq",
    "IE": "Ireland", "IL": "Israel", "IT": "Italy", "JM": "Jamaica",
    "JP": "Japan", "JO": "Jordan", "KZ": "Kazakhstan", "KE": "Kenya",
    "KW": "Kuwait", "KG": "Kyrgyzstan", "LV": "Latvia", "LB": "Lebanon",
    "LY": "Libya", "LT": "Lithuania", "LU": "Luxembourg", "MY": "Malaysia",
    "MT": "Malta", "MX": "Mexico", "MD": "Moldova", "MA": "Morocco",
    "MZ": "Mozambique", "MM": "Myanmar", "NP": "Nepal", "NL": "Netherlands",
    "NZ": "New Zealand", "NI": "Nicaragua", "NG": "Nigeria", "NO": "Norway",
    "OM": "Oman", "PK": "Pakistan", "PA": "Panama", "PG": "Papua New Guinea",
    "PY": "Paraguay", "PE": "Peru", "PH": "Philippines", "PL": "Poland",
    "PT": "Portugal", "PR": "Puerto Rico", "QA": "Qatar", "RO": "Romania",
    "RU": "Russia", "SA": "Saudi Arabia", "SN": "Senegal", "RS": "Serbia",
    "SG": "Singapore", "SK": "Slovakia", "ZA": "South Africa",
    "KR": "South Korea", "ES": "Spain", "LK": "Sri Lanka", "SD": "Sudan",
    "SE": "Sweden", "CH": "Switzerland", "SY": "Syria", "TW": "Taiwan",
    "TZ": "Tanzania", "TH": "Thailand", "TT": "Trinidad and Tobago",
    "TN": "Tunisia", "TR": "Turkey", "UG": "Uganda", "UA": "Ukraine",
    "AE": "United Arab Emirates", "GB": "United Kingdom", "US": "United States",
    "UY": "Uruguay", "UZ": "Uzbekistan", "VE": "Venezuela", "VN": "Vietnam",
    "YE": "Yemen", "ZW": "Zimbabwe", "PS": "Palestine",
}


def country_code_to_name(code: str) -> str:
    """Return the full country name for a 2-letter ISO code, or empty string."""
    return COUNTRY_CODE_TO_NAME.get((code or "").upper(), "")


# ── Main filter function ──────────────────────────────────────────────────────

def should_skip(account: dict, filters: dict, blacklist: set,
                _skip_reason: list = None) -> bool:
    """
    Returns True if the account should be skipped / blacklisted.
    account dict keys: username, full_name, bio, is_private,
                       has_profile_pic, post_count, has_recent_post,
                       has_story, email, phone

    If _skip_reason is a list, the reason string is appended to it so the
    caller can log exactly which filter triggered the skip.
    """
    def _reject(reason: str) -> bool:
        if _skip_reason is not None:
            _skip_reason.append(reason)
        return True

    username = account.get("username", "").lower()

    # Blacklist check
    if username in blacklist:
        return _reject("blacklisted")

    # Private
    if filters.get("skip_private") and account.get("is_private", False):
        return _reject("private account")

    # No bio
    if filters.get("skip_no_bio") and not account.get("bio", "").strip():
        return _reject("no bio")

    # No profile pic
    if filters.get("skip_no_profile_pic") and not account.get("has_profile_pic", True):
        return _reject("no profile pic")

    # No contact info (email or phone)
    if filters.get("skip_no_contact"):
        has_email = bool(account.get("email", "").strip())
        has_phone = bool(account.get("phone", "").strip())
        if not has_email and not has_phone:
            return _reject("no email and no phone")

    # Minimum posts
    min_posts = int(filters.get("min_posts", 0))
    if min_posts > 0:
        post_count = account.get("post_count", 0)
        try:
            post_count = int(post_count)
        except (ValueError, TypeError):
            post_count = 0
        if post_count < min_posts:
            return _reject(f"post count {post_count} < min {min_posts}")

    # Recency (no recent post AND no active story)
    # Only active if explicitly set to a positive value in filters AND the
    # "enable_post_spin" checkbox is OFF (i.e. the old simple recency flag).
    # When enable_post_spin is ON, recency is handled by the months-threshold
    # block below; we must NOT double-fire this older check or it will skip
    # accounts that have already been confirmed recent by the post-spin.
    req_days = int(filters.get("require_recent_post_days", 0))
    if req_days > 0 and not filters.get("enable_post_spin", False):
        has_recent = account.get("has_recent_post", True)
        has_story = account.get("has_story", False)
        if not has_recent and not has_story:
            return _reject(f"no recent post (req_days={req_days})")

    # Check for "skip_no_posts_last_n_months"
    # Only active when the "Enable post-spin" checkbox is ON in the UI.
    # The actual date parsing and age calculation already happened inside
    # open_profile_details() which set has_recent_post=False when the post
    # was older than the threshold.  We simply trust that flag here — we do
    # NOT re-parse the raw date text because it may contain suffixes like
    # "• See translation" that break silent parsing.
    enable_post_spin = filters.get("enable_post_spin", False)
    months_threshold = int(filters.get("skip_no_posts_last_n_months", 0))
    if enable_post_spin and months_threshold > 0 and not account.get("has_story", False):
        if account.get("has_recent_post") is False:
            return _reject(
                f"post too old (> {months_threshold} month(s))"
            )

    # Keyword blacklist
    keywords = filters.get("keywords", [])
    if keywords:
        haystack = " ".join([
            account.get("username", ""),
            account.get("full_name", ""),
            account.get("bio", ""),
        ]).lower()
        for kw in keywords:
            if kw.strip().lower() and kw.strip().lower() in haystack:
                return _reject(f"keyword match: {kw!r}")

    return False


def parse_keywords(raw: str) -> List[str]:
    """Parse comma and/or newline separated keywords."""
    if not raw.strip():
        return []
    normalized = raw.replace("\r\n", ",").replace("\n", ",").replace("\r", ",")
    return [p.strip() for p in normalized.split(",") if p.strip()]