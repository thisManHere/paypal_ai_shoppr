import os
import requests
from catalog import CATALOGS

SEARCH_URL = "https://api.trychannel3.com/v1/search"

class Channel3Error(Exception):
    pass

def the_price(offer):
    p = offer.get("price")
    if isinstance(p, dict):
        return p.get("price"), p.get("currency")
    return p, offer.get("currency")

def normalize(p):
    """Turn Channel3's product into a small, predictable shape our app can rely on."""
    offers = p.get("offers") or []
    best = offers[0] if offers else {}
    price, currency = the_price(best)
    image = p.get("image_url")
    if not image:
        imgs = p.get("images") or []
        if imgs:
            image = imgs[0].get("url") if isinstance(imgs[0], dict) else imgs[0]
    brand = p.get("brand_name") or p.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    return {"id": p.get("id"), "title": p.get("title"), "brand": brand, "image": image,
            "price": price, "currency": currency, "store": best.get("domain"), "url": best.get("url")
            }


def search_products(query, steps, limit=6):
    key = os.getenv("CHANNEL3_API_KEY")
    if not key:
        raise Channel3Error("CHANNEL3_API_KEY is missing from your .env file.")
    steps.append("Read the Channel3 API key from .env")
    steps.append(f'Sent POST {SEARCH_URL} with query "{query}"')
    r = requests.post(SEARCH_URL, headers={"x-api-key": key, "Content-Type": "application/json"},
                      json={"query": query}, timeout=30)
    steps.append(f"Channel3 answered with status {r.status_code}")
    if r.status_code in (401, 403):
        raise Channel3Error("Channel3 rejected the API key. Re-copy it into .env and restart the server.")
    if r.status_code != 200:
        raise Channel3Error(f"Channel3 returned status {r.status_code}: {r.text[:200]}")
    raw = r.json().get("products", [])
    steps.append(f"Channel3 found {len(raw)} products; we show the first {min(limit, len(raw))}")
    products = [normalize(p) for p in raw[:limit]]
    for p in products:
        CATALOGS[str(p["id"])] = p  
    return products, (raw[0] if raw else None)

