import os 
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory 

load_dotenv()

from paypal_client import get_access_token, PayPalError, create_order, capture_order, verify_webhook
from channel3_client import search_products, Channel3Error
from llm_client import run_self_test, LLMError
from catalog import CATALOGS, ORDERS, SEEN_EVENT_IDS, EVENTS
from datetime import datetime, timezone
from agent import run_agent
import openai

from werkzeug.middleware.proxy_fix import ProxyFix
from pages import bp as pages_bp

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.register_blueprint(pages_bp)


@app.route("/")
def home():
    return send_from_directory("public", "index.html")



@app.route("/api/health")
def health_check():
    return jsonify(ok=True, keys={
        "paypal": bool(os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET")),
        "channel3": bool(os.environ.get("CHANNEL3_API_KEY")),
        "llm": bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    })

@app.route("/api/paypal/test")
def paypal_test():
    steps = []
    try:
        token, lifetime = get_access_token()
        return jsonify(ok=True, steps=steps, token_preview=token[:8] + "…", lifetime=lifetime)
    except PayPalError as e:
        return jsonify(ok=False, steps=steps, error=str(e)), 400
    except requests.RequestException as e:
        return jsonify(ok=False, steps=steps, error=f"Could not reach PayPal (network problem): {e}"), 502


@app.route("/api/products/search", methods=["POST"])
def products_search():
    steps = []
    query = (request.get_json(silent=True) or {}).get("query", "").strip()
    if not query:
        return jsonify(ok=False, steps=steps, error="Type what you want to search for."), 400
    try:
        products, first_raw = search_products(query, steps)
        return jsonify(ok=True, steps=steps, products=products, raw_first_product=first_raw)
    except Channel3Error as e:
        return jsonify(ok=False, steps=steps, error=str(e)), 400
    except requests.RequestException as e:
        return jsonify(ok=False, steps=steps, error=f"Could not reach Channel3 (network problem): {e}"), 502

@app.route("/api/config")
def config():
    return jsonify(paypal_client_id=os.getenv("PAYPAL_CLIENT_ID"))

@app.route("/api/llm/test")
def llm_test():
    steps = []
    try:
        result = run_self_test(steps)
        return jsonify(ok=True, steps=steps, **result)
    except LLMError as e:
        return jsonify(ok=False, steps=steps, error=str(e)), 400
    except openai.AuthenticationError:
        return jsonify(ok=False, steps=steps, error="The API key was rejected. Copy your Gemini key into .env and restart."), 400
    except openai.NotFoundError:
        return jsonify(ok=False, steps=steps, error="That model name was not found. Check the current model ID in Google AI Studio and set LLM_MODEL in .env."), 400
    except openai.RateLimitError:
        return jsonify(ok=False, steps=steps, error="Rate limit reached (free tier). Wait a minute and try again."), 429
    except openai.OpenAIError as e:
        return jsonify(ok=False, steps=steps, error=f"AI call failed: {e}"), 502

@app.route("/api/paypal/capture-order", methods=["POST"])
def paypal_capture_order():
    steps = []
    order_id = str((request.get_json(silent=True) or {}).get("order_id", ""))
    if order_id not in ORDERS:
        return jsonify(ok=False, steps=steps, error="Unknown order. It may have been lost when the server restarted."), 404
    try:
        result = capture_order(order_id, steps)
        ORDERS[order_id].update(status=result["status"], capture=result)
        return jsonify(ok=True, steps=steps, product=ORDERS[order_id]["product"], **result)
    except PayPalError as e:
        return jsonify(ok=False, steps=steps, error=str(e)), 400
    except requests.RequestException as e:
        return jsonify(ok=False, steps=steps, error=f"Could not reach PayPal (network problem): {e}"), 502
    
@app.route("/api/paypal/create-order", methods=["POST"])
def paypal_create_order():
    steps = []
    product_id = str((request.get_json(silent=True) or {}).get("product_id", ""))
    product = CATALOGS.get(product_id)
    if not product:
        return jsonify(ok=False, steps=steps, error="We don't recognise that product. Ask the concierge again."), 404
    try:
        order_id, amount = create_order(product, steps)
        ORDERS[order_id] = {"product": product, "amount": amount, "status": "CREATED"}
        return jsonify(ok=True, id=order_id, steps=steps)
    except PayPalError as e:
        return jsonify(ok=False, steps=steps, error=str(e)), 400
    except requests.RequestException as e:
        return jsonify(ok=False, steps=steps, error=f"Could not reach PayPal (network problem): {e}"), 502

def _order_id_from(event):
    resource = event.get("resource") or {}
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    return related.get("order_id")
 
 
def _log_event(event, verified, note):
    EVENTS.append({"id": event.get("id"), "type": event.get("event_type"), "verified": verified,
                   "note": note, "order_id": _order_id_from(event),
                   "time": datetime.now(timezone.utc).strftime("%H:%M:%S UTC")})
    del EVENTS[:-50]

@app.route("/api/paypal/webhook", methods=["POST"])
def paypal_webhook():
    """PayPal calls THIS address by itself when something happens (e.g. a payment completes)."""
    event = request.get_json(silent=True)
    if not isinstance(event, dict) or not event.get("id"):
        return jsonify(ok=False, error="Not a PayPal event"), 400
    if event["id"] in SEEN_EVENT_IDS:
        return jsonify(ok=True, duplicate=True)          # PayPal sometimes repeats messages
    steps = []
    try:
        verified = verify_webhook(request.headers, event, steps)
    except PayPalError as e:
        _log_event(event, False, str(e))
        return jsonify(ok=False, error=str(e)), 400
    except requests.RequestException:
        return jsonify(ok=False, error="Could not reach PayPal to verify"), 500   # a 5xx makes PayPal retry later
    _log_event(event, verified, "" if verified else "Signature did not match")
    if not verified:
        return jsonify(ok=False, error="Signature not valid"), 400
    SEEN_EVENT_IDS.add(event["id"])
    if event.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
        order_id = _order_id_from(event)
        if order_id in ORDERS:
            ORDERS[order_id]["webhook_confirmed"] = True
    return jsonify(ok=True)

@app.route("/api/events")
def events():
    orders = [{"order_id": oid, "title": o["product"].get("title"), "amount": o["amount"],
               "status": o["status"], "webhook_confirmed": o.get("webhook_confirmed", False)}
              for oid, o in ORDERS.items()]
    return jsonify(ok=True, events=EVENTS[::-1][:20], orders=orders[::-1])

@app.route("/api/agent/chat", methods=["POST"])
def agent_chat():
    steps = []
    message = (request.get_json(silent=True) or {}).get("message", "").strip()
    if not message:
        return jsonify(ok=False, steps=steps, error="Tell the concierge what you are looking for."), 400
    try: 
        result = run_agent(message, steps)
        return jsonify(ok=True, steps=steps, **result)
    except LLMError as e:
        return jsonify(ok=False, steps=steps, error=str(e)), 400
    except openai.AuthenticationError:
        return jsonify(ok=False, steps=steps, error="The AI key was rejected. Check GEMINI_API_KEY in .env. Check if it is connected properly to the openAI."), 400
    except openai.NotFoundError:
        return jsonify(ok=False, steps=steps, error="Model name not found. Check LLM_MODEL in .env."), 400
    except openai.RateLimitError:
        return jsonify(ok=False, steps=steps, error="AI rate limit reached. Wait a minute and try again."), 429
    except openai.OpenAIError as e:
        return jsonify(ok=False, steps=steps, error=f"AI call failed: {e}"), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)