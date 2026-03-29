"""
Filter accounts based on configurable rules.
Supports: keywords, no-bio, private, no-contact, min posts, no profile pic, story detection.
"""
import re
from typing import List


# ── Contact extraction ────────────────────────────────────────────────────────

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+",
    re.IGNORECASE,
)

PHONE_RE = re.compile(
    r"(?:\+?[\d\s\-\(\)]{7,20})",
)

# Country-code prefixes (international dialing codes → ISO 2-letter)
COUNTRY_PHONE_PREFIXES = {
    "1": "US", "44": "GB", "49": "DE", "33": "FR", "39": "IT",
    "34": "ES", "7": "RU", "81": "JP", "86": "CN", "91": "IN",
    "55": "BR", "52": "MX", "61": "AU", "64": "NZ", "27": "ZA",
    "20": "EG", "966": "SA", "971": "AE", "962": "JO", "965": "KW",
    "974": "QA", "973": "BH", "968": "OM", "212": "MA", "213": "DZ",
    "216": "TN", "218": "LY", "249": "SD", "251": "ET", "234": "NG",
    "254": "KE", "255": "TZ", "256": "UG", "263": "ZW", "260": "ZM",
    "82": "KR", "66": "TH", "62": "ID", "60": "MY", "63": "PH",
    "84": "VN", "65": "SG", "92": "PK", "880": "BD", "94": "LK",
    "98": "IR", "90": "TR", "972": "IL", "30": "GR", "48": "PL",
    "31": "NL", "32": "BE", "41": "CH", "43": "AT", "46": "SE",
    "47": "NO", "45": "DK", "358": "FI", "351": "PT", "353": "IE",
    "420": "CZ", "36": "HU", "40": "RO", "380": "UA", "375": "BY",
    "371": "LV", "370": "LT", "372": "EE", "54": "AR", "56": "CL",
    "57": "CO", "51": "PE", "58": "VE", "593": "EC", "595": "PY",
    "598": "UY", "591": "BO",
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
    # Look for numbers with country code prefix or local format
    matches = PHONE_RE.findall(text)
    for m in matches:
        cleaned = re.sub(r"[\s\-\(\)]", "", m)
        if len(cleaned) >= 7:
            return cleaned
    return ""


def infer_country_code(phone: str, location_text: str = "") -> str:
    """
    Try to infer 2-letter country code from phone prefix or location text.
    Returns empty string if cannot infer.
    """
    if phone:
        cleaned = re.sub(r"[^\d]", "", phone)
        if cleaned.startswith("00"):
            cleaned = cleaned[2:]
        elif cleaned.startswith("+"):
            cleaned = cleaned[1:]
        # Try longest prefix first (3 digits, then 2, then 1)
        for length in (3, 2, 1):
            prefix = cleaned[:length]
            if prefix in COUNTRY_PHONE_PREFIXES:
                return COUNTRY_PHONE_PREFIXES[prefix]

    # Simple keyword match on location text
    COUNTRY_KEYWORDS = {
        "united states": "US", "usa": "US", "u.s.a": "US",
        "united kingdom": "GB", "uk": "GB", "england": "GB",
        "germany": "DE", "deutschland": "DE",
        "france": "FR", "spain": "ES", "italy": "IT",
        "russia": "RU", "japan": "JP", "china": "CN", "india": "IN",
        "brazil": "BR", "mexico": "MX", "australia": "AU",
        "canada": "CA", "egypt": "EG", "saudi": "SA",
        "uae": "AE", "emirates": "AE", "dubai": "AE",
        "jordan": "JO", "kuwait": "KW", "qatar": "QA",
        "morocco": "MA", "algeria": "DZ", "tunisia": "TN",
        "nigeria": "NG", "south africa": "ZA", "kenya": "KE",
        "korea": "KR", "thailand": "TH", "indonesia": "ID",
        "malaysia": "MY", "philippines": "PH", "vietnam": "VN",
        "singapore": "SG", "pakistan": "PK", "bangladesh": "BD",
        "iran": "IR", "turkey": "TR", "israel": "IL",
        "greece": "GR", "poland": "PL", "netherlands": "NL",
        "belgium": "BE", "switzerland": "CH", "austria": "AT",
        "sweden": "SE", "norway": "NO", "denmark": "DK",
        "finland": "FI", "portugal": "PT", "ireland": "IE",
        "argentina": "AR", "chile": "CL", "colombia": "CO",
        "peru": "PE", "venezuela": "VE",
    }
    loc_lower = location_text.lower()
    for keyword, code in COUNTRY_KEYWORDS.items():
        if keyword in loc_lower:
            return code
    return ""


# ── Main filter function ──────────────────────────────────────────────────────

def should_skip(account: dict, filters: dict, blacklist: set) -> bool:
    """
    Returns True if the account should be skipped / blacklisted.
    account dict keys: username, full_name, bio, is_private,
                       has_profile_pic, post_count, has_recent_post,
                       has_story, email, phone
    """
    username = account.get("username", "").lower()

    # Blacklist check
    if username in blacklist:
        return True

    # Private
    if filters.get("skip_private") and account.get("is_private", False):
        return True

    # No bio
    if filters.get("skip_no_bio") and not account.get("bio", "").strip():
        return True

    # No profile pic
    if filters.get("skip_no_profile_pic") and not account.get("has_profile_pic", True):
        return True

    # No contact info (email or phone)
    if filters.get("skip_no_contact"):
        has_email = bool(account.get("email", "").strip())
        has_phone = bool(account.get("phone", "").strip())
        if not has_email and not has_phone:
            return True

    # Minimum posts
    min_posts = int(filters.get("min_posts", 0))
    if min_posts > 0:
        post_count = account.get("post_count", 0)
        try:
            post_count = int(post_count)
        except (ValueError, TypeError):
            post_count = 0
        if post_count < min_posts:
            return True

    # Recency (no recent post AND no active story)
    req_days = int(filters.get("require_recent_post_days", 365))
    if req_days > 0:
        has_recent = account.get("has_recent_post", True)
        has_story = account.get("has_story", False)
        if not has_recent and not has_story:
            return True

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
                return True

    return False


def parse_keywords(raw: str) -> List[str]:
    """Parse comma and/or newline separated keywords."""
    if not raw.strip():
        return []
    normalized = raw.replace("\r\n", ",").replace("\n", ",").replace("\r", ",")
    return [p.strip() for p in normalized.split(",") if p.strip()]
