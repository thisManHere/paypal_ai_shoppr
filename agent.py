import json 
import requests
from llm_client import get_client
from channel3_client import search_products, Channel3Error

max_turns = 4
max_searches = 2

SYSTEM_PROMPT = """You are a friendly shopping concierge inside a payments app.
Rules:
1. Turn the customer's request into a short, good search query and call search_products. You may search at most twice.
2. Only recommend products that appeared in the search results. Never invent products, prices or ids.
3. Respect the customer's budget. Skip products with no price or a price over budget.
4. Final answer format: first a short friendly paragraph (2 or 3 sentences). Then up to 3 lines, each exactly:
PICK|<product id>|<one sentence on why it fits>
5. Do not use markdown."""

TOOLS = [{
    "type": "function",
    "function": {
        "name": "search_products",
        "description": "Search real products from online stores. Returns id, title, brand, price, currency, store.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "short product search words"}},
                       "required": ["query"]},
    },
}]

def compact_products(products):
    """Give the AI only the fields it needs"""
    keys = ("id", "title", "brand", "price", "currency", "store")
    return [{k: p.get(k) for k in keys} for p in products]

def run_tool(call, found, steps, state):
    """The AI can only ask for a tool. This is the function we call for when we want our code to do that"""
    if call.function.name != "search_products":
        return "Unknown_tool"
    try:
        query = (json.loads(call.function.arguments or "{}").get("query") or "").strip()
    except json.JSONDecodeError:
        return "Bad arguments. Send JSON with a 'query' text."
    if not query:
        return "Missing query"
    if state["searches"] >= max_searches:
        steps.append("Search limit reached, so the AI will answer what it already has")
        return "Search limit reached. Answer using the products you already have"
    state["searches"] += 1
    steps.append(f'The AI asked to run search_products("{query}")')
    try:
        products, _ = search_products(query, steps, limit=8)
    except (Channel3Error, requests.RequestException) as e:
        steps.append(f"The search failed: {e}")
        return f"The search failed: {e}"
    for p in products:
        found[str(p["id"])] = p
    return json.dumps(compact_products(products))

def finished_tool(text, found):
    """Split the AI's answer into the friendly paragraph and the pick lines."""
    picks, other = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("PICK|"):
            parts = s.split("|", 2)
            if len(parts) == 3 and parts[1].strip() in found:
                picks.append({**found[parts[1].strip()], "reason": parts[2].strip()})
        else:
            other.append(line)
    if not picks:
        picks = [{**p, "reason": ""} for p in list(found.values())[:3]]
    return {"reply": "\n".join(other).strip(), "picks": picks}

def run_agent(user_message, steps):
    client, model, provider = get_client()
    steps.append(f"Using {provider}, model '{model}'")
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}]
    found, state = {}, {"searches": 0}
 
    for turn in range(max_turns):
        steps.append(f"Asked the AI what to do next (round {turn + 1})")
        resp = client.chat.completions.create(model=model, messages=messages, tools=TOOLS, tool_choice="auto")
        msg = resp.choices[0].message
        if not msg.tool_calls:
            steps.append("The AI wrote its final answer")
            return finished_tool(msg.content or "", found)
        messages.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            result = run_tool(call, found, steps, state)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        steps.append("Reached the round limit, so we stopped the loop")
    return finished_tool("I searched but ran out of steps. Here is what I found.", found)
