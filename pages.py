import os
import requests
import openai
from flask import Blueprint, render_template, request, redirect, url_for
from paypal_client import get_access_token, create_order, capture_order, PayPalError
from channel3_client import search_products, Channel3Error
from llm_client import run_self_test, LLMError
from agent import run_agent
from catalog import CATALOGS, ORDERS, EVENTS

bp = Blueprint("pages", __name__)
DEFAULT_MESSAGE = "I need a birthday gift for my mom who loves painting. Budget is R1000."

def explained(e):
    """Turn any known error into a plain sentence. Unknown errors will then be raised again so we notice them."""
    if isinstance(e, (PayPalError, Channel3Error, LLMError)):
        return str(e)
    if isinstance(e, openai.AuthenticationError):
        return "The AI key was rejected. Check GEMINI_API_KEY."
    if isinstance(e, openai.NotFoundError):
        return "Model name not found. Check LLM_MODEL."
    if isinstance(e, openai.RateLimitError):
        return "AI rate limit reached. Wait a minute and try again."
    if isinstance(e, openai.OpenAIError):
        return f"AI call failed: {e}"
    if isinstance(e, requests.RequestException):
        return f"Network problem reaching an outside service: {e}"
    raise e

def message(title, state, lines, steps=None, code=200):
    return render_template("message.html", title=title, state=state, lines=lines, trace_steps=steps or []), code

@bp.route("/", methods=["GET", "POST"])
def concierge():
    ctx = {"message": DEFAULT_MESSAGE, "result": None, "error": None, "steps_list": []}
    if request.method == "POST":
        ctx["message"] = request.form.get("message", "").strip()
        if not ctx["message"]:
            ctx["error"] = "Tell the concierge what you are hunting for."
        else:
            try:
                ctx["result"] = run_agent(ctx["message"], ctx["steps_list"])
            except Exception as e:
                ctx["error"] = explained(e)
    return render_template("concierge.html", **ctx)

@bp.route("/checkout", methods=["POST"])
def checkout():
    """Create a PayPal order, then send the browser to PayPal to approve it."""
    steps = []
    product = CATALOGS.get(request.form.get("product_id", ""))   
    if not product:
        return message("Unknown product", "bad", ["We don't recognise that product. Hunt again first."], code=404)
    source = {"paypal": {"experience_context": {
        "brand_name": "AI Shopping Concierge", "user_action": "PAY_NOW",
        "return_url": url_for("pages.paypal_return", _external=True),
        "cancel_url": url_for("pages.paypal_cancel", _external=True)}}}
    links = []
    try:
        order_id, amount = create_order(product, steps, payment_source=source, links_out=links)
    except Exception as e:
        return message("Checkout failed", "bad", [explained(e)], steps, 400)
    ORDERS[order_id] = {"product": product, "amount": amount, "status": "CREATED"}
    approve = next((l["href"] for l in links if l.get("rel") in ("payer-action", "approve")), None)
    if not approve:
        return message("Checkout failed", "bad", ["PayPal did not return an approval link."], steps, 502)
    return redirect(approve, code=303)
 
 
@bp.route("/paypal/return")
def paypal_return():
    """PayPal sends the buyer back here after they approve. We now capture the payment."""
    order_id = request.args.get("token", "")
    order = ORDERS.get(order_id)
    if not order:
        return message("Unknown order", "bad", ["We have no record of this order. The server may have restarted."], code=404)
    steps = []
    if order.get("capture"):
        steps.append("This order was already captured, so we show the saved receipt")
        result = order["capture"]
    else:
        try:
            result = capture_order(order_id, steps)
        except Exception as e:
            return message("Payment failed", "bad", [explained(e)], steps, 400)
        order.update(status=result["status"], capture=result)
    lines = [f'{order["product"].get("title")}', f'Paid {result["amount"]} {result["currency"]}',f'PayPal status: {result["status"]}', f'Capture ID: {result["capture_id"]}',
             f'Buyer: {result.get("buyer_email") or "n/a"}']
    return message("Payment complete", "ok", lines, steps)

@bp.route("/paypal/cancel")
def paypal_cancel():
    return message("Payment cancelled", "idle", ["You left PayPal without paying. Nothing was charged."])
 
 
@bp.route("/orders")
def orders():
    rows = [{"order_id": oid, "title": o["product"].get("title"), "amount": o["amount"],
             "status": o["status"], "webhook_confirmed": o.get("webhook_confirmed", False)}
            for oid, o in ORDERS.items()]
    return render_template("orders.html", orders=rows[::-1], events=EVENTS[::-1][:20])
 
 
CHECK_NAMES = {"paypal": "PayPal login", "ai": "AI model", "search": "Product search"}

@bp.route("/status", methods=["GET", "POST"])
def status():
    keys = [("PayPal Client ID and Secret", bool(os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_CLIENT_SECRET"))),
            ("Channel3 API key", bool(os.getenv("CHANNEL3_API_KEY"))),
            ("AI key (Gemini)", bool(os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY"))),
            ("PayPal Webhook ID", bool(os.getenv("PAYPAL_WEBHOOK_ID")))]
    check = None
    if request.method == "POST":
        which = request.form.get("check", "")
        steps = []
        check = {"name": CHECK_NAMES.get(which, "Unknown check"), "ok": False, "detail": "Unknown check.", "steps": steps}
        try:
            if which == "paypal":
                token, life = get_access_token(steps)
                check.update(ok=True, detail=f"Token received (starts with {token[:8]}…), valid for {life} seconds.")
            elif which == "ai":
                r = run_self_test(steps)
                check.update(ok=True, detail=f'The AI said: "{r["hello"]}". Tool request: {r["tool_call"] or "none this time"}')
            elif which == "search":
                products, _ = search_products("drawing canvas", steps, limit=3)
                check.update(ok=True, detail=f"Found {len(products)} products: " + "; ".join((p["title"] or "?") for p in products))
        except Exception as e:
            check["detail"] = explained(e)
    return render_template("status.html", keys=keys, check=check)
