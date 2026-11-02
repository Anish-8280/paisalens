"""Rule-based merchant cleaning and categorisation. LLM fallback comes later, measured against this."""
import re

# Keyword found in the upper-cased payee -> (clean merchant name, category).
MERCHANTS: dict[str, tuple[str, str]] = {
    "SWIGGY INSTAMART": ("Instamart", "Groceries"),
    "SWIGGY": ("Swiggy", "Food Delivery"),
    "ZOMATO": ("Zomato", "Food Delivery"),
    "BIGBASKET": ("BigBasket", "Groceries"),
    "BLINKIT": ("Blinkit", "Groceries"),
    "DMART": ("DMart", "Groceries"),
    "AMAZON": ("Amazon", "Shopping"),
    "AMZN": ("Amazon", "Shopping"),
    "FLIPKART": ("Flipkart", "Shopping"),
    "MYNTRA": ("Myntra", "Shopping"),
    "NETFLIX": ("Netflix", "Subscriptions"),
    "SPOTIFY": ("Spotify", "Subscriptions"),
    "HOTSTAR": ("Hotstar", "Subscriptions"),
    "UBER": ("Uber", "Transport"),
    "OLA": ("Ola", "Transport"),
    "IRCTC": ("IRCTC", "Travel"),
    "INDIGO": ("IndiGo", "Travel"),
    "AIRTEL": ("Airtel", "Bills"),
    "JIO": ("Jio", "Bills"),
    "BESCOM": ("BESCOM", "Bills"),
    "APOLLO": ("Apollo Pharmacy", "Health"),
    "HPCL": ("HPCL", "Fuel"),
    "INDIAN OIL": ("Indian Oil", "Fuel"),
    "PRESTIGE ESTATES": ("Rent", "Rent"),
}

# Words that mark an unknown UPI payee as a shop, not a person.
SHOP_WORDS = {"STORE", "STORES", "MART", "CAFE", "RESTAURANT", "KIRANA", "BAKERY", "MEDICAL", "HOTEL", "SALON"}
LEGAL_SUFFIXES = re.compile(r"\b(PRIVATE LIMITED|PVT\.? LTD\.?|PVT|LIMITED|LTD\.?|LLP|INDIA)\b\.?", re.I)
MAX_MERCHANT_LENGTH = 40


def lookup_merchant(payee: str) -> tuple[str, str] | None:
    upper = payee.upper()
    for keyword, known in MERCHANTS.items():
        if re.search(rf"\b{re.escape(keyword)}\b", upper):
            return known
    return None


def looks_like_shop(payee: str) -> bool:
    return bool(SHOP_WORDS & set(re.findall(r"[A-Z]+", payee.upper())))


def clean_name(payee: str) -> str:
    """Tidy a free-text payee. The length cap and character filter also blunt text injected via remarks."""
    name = LEGAL_SUFFIXES.sub("", payee)
    name = re.sub(r"[^A-Za-z0-9&' ]+", " ", name)
    name = " ".join(name.split()).title()
    return name[:MAX_MERCHANT_LENGTH].strip() or "Unknown"
