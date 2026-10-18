import os
import time
import uuid
import requests
from decimal import Decimal, InvalidOperation

BASE = "https://api-m.sandbox.paypal.com"
saved_tokens = {"token": None, "expires_at": 0} 

class PayPalError(Exception):
    pass

def get_access_token(steps):
    """Get a new access token from PayPal, or trade our Client ID + Secret for a temporary access token.
    `steps` is a list we append simple English notes to, so the dashboard can show what happened.    
    """
    client_id = os.getenv("PAYPAL_CLIENT_ID")
    secret = os.getenv("PAYPAL_CLIENT_SECRET")
    if not client_id or not secret:
        raise PayPalError("PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET is missing from your .env file.")
    steps.append("Read PayPal Client ID and secret from .env file.")

    if saved_tokens["token"] and time.time() < saved_tokens["expires_at"] - 60:
        steps.append("Reused the token we saved earlier")
        return saved_tokens["token"], int(saved_tokens["expires_at"] - time.time())

    steps.append(f"Sent POST {BASE}/v1/oauth2/token with our credentials")
    r = requests.post(
        f"{BASE}/v1/oauth2/token",
        auth=(client_id, secret),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=20,
    )

    steps.append(f"PayPal answered with status {r.status_code}")
    if r.status_code != 200:
        raise PayPalError("PayPal rejected the credentials. Make sure you copied the SANDBOX Client ID and Secret correctly")
    data = r.json()
    lifetime = int(data.get("expires_in", 300))
    saved_tokens["token"] = data.get("access_token")
    saved_tokens["expires_at"] = time.time() + lifetime
    steps.append(f"Received an access token that works for {lifetime} seconds")
    return data["access_token"], lifetime

def auth_headers(steps):
    token, _ = get_access_token(steps)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def error_text(r):
    try:
        j = r.json()
        return j.get("message") or j.get("error_description") or r.text[:200]
    except ValueError:
        return r.text[:200]

def create_order(product, steps):
    """Inform PayPal: 'A customer would like to pay this amount. ' PayPal will then answer with the order ID."""
    currency = (product.get("currency") or "ZAR").upper()
    if currency != "ZAR":
        raise PayPalError(f"This demo checks out in South African Rands ZAR only, and this item is priced in {currency}.")
    try:
        amount = Decimal(str(product.get("price"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        raise PayPalError("This product has no usable price.")
    if amount <= 0:
        raise PayPalError("This product has no usable price.")

    headers = auth_headers(steps)
    headers["PayPal-Request-Id"] = str(uuid.uuid4())

    body = {"intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": str(product["id"])[:200],
                "description": (product.get("title") or "Item")[:127],
                "amount": {"currency_code": "USD", "value": str(amount)}
                }]
            }
    
    steps.append(f"Sent POST {BASE}/v2/checkout/orders for {amount} ZAR (price taken from the server's own list)")
    r = requests.post(f"{BASE}/v2/checkout/orders", json=body, headers=headers, timeout=20)
    steps.append(f"PayPal answered with status {r.status_code}")
    if r.status_code not in (200, 201):
        raise PayPalError(f"PayPal could not create the order: {error_text(r)}")
    order_id = r.json()["id"]
    steps.append(f"PayPal created order {order_id} and is waiting for the buyer to approve it")
    return order_id, str(amount)

def capture_order(order_id, steps):
    """The money will be moved by capture as soon as the buyer approves."""
    headers = auth_headers(steps)
    steps.append(f"Sent POST {BASE}/v2/checkout/orders/{order_id}/capture")
    r = requests.post(f"{BASE}/v2/checkout/orders/{order_id}/capture", headers=headers, json={}, timeout=20)
    steps.append(f"PayPal answered with status {r.status_code}")
    if r.status_code not in (200, 201):
        raise PayPalError(f"PayPal could not complete the payment: {error_text(r)}")
    data = r.json()
    try:
        cap = data["purchase_units"][0]["payments"]["captures"][0]
        payer = data.get("payer", {})
        result = {"status": data.get("status"), "capture_id": cap.get("id"),
                  "capture_status": cap.get("status"), "amount": cap["amount"]["value"],
                  "currency": cap["amount"]["currency_code"], "buyer_email": payer.get("email_address")
                }
    except (KeyError, IndexError):
        raise PayPalError("Payment went through but PayPal's reply had an unexpected shape.")

    steps.append(f"Payment {result['status']}. Capture ID {result['capture_id']}")
    return result

def verify_webhook(headers, event, steps):
    """Ask PayPal: 'did YOU really send this message?' Anyone can POST to our public address,
    so we must never trust a webhook until PayPal confirms its signature."""
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        raise PayPalError("PAYPAL_WEBHOOK_ID is missing from the environment variables.")
    parts = {
        "auth_algo": headers.get("PAYPAL-AUTH-ALGO"),
        "cert_url": headers.get("PAYPAL-CERT-URL"),
        "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID"),
        "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG"),
        "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME"),
    }
    if not all(parts.values()):
        raise PayPalError("Missing PayPal signature headers. This message did not come from PayPal.")
    body = {**parts, "webhook_id": webhook_id, "webhook_event": event}
    steps.append("Asked PayPal to verify the signature of this webhook")
    r = requests.post(f"{BASE}/v1/notifications/verify-webhook-signature",
                      json=body, headers=auth_headers(steps), timeout=20)
    if r.status_code != 200:
        raise PayPalError(f"PayPal's verification service answered {r.status_code}: {error_text(r)}")
    status = r.json().get("verification_status")
    steps.append(f"PayPal said: {status}")
    return status == "SUCCESS"
 