import os
import requests


def notify_zapier(order_id, order):
    url = os.getenv("ZAPIER_HOOK_URL")         
    if not url:
        return "skipped (no ZAPIER_HOOK_URL set)"
    capture = order.get("capture") or {}
    product = order.get("product") or {}
    payload = {
        "order_id": order_id,
        "item": product.get("title"),
        "store": product.get("store"),
        "product_url": product.get("url"),
        "amount": order.get("amount"),
        "currency": "USD",
        "capture_id": capture.get("capture_id"),
        "buyer_email": capture.get("buyer_email")
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        return "sent" if r.ok else f"failed (status {r.status_code})"
    except requests.RequestException as e:
        return f"failed ({type(e).__name__})"