import os
from openai import OpenAI

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class LLMError(Exception):
    pass

def get_client():
    """Returns client, model_name, provider_label. Every other file calls this one function."""
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        client = OpenAI(api_key=gemini_key, base_url=GEMINI_URL)
        return client, os.getenv("LLM_MODEL", "gemini-3.5-flash"), "Gemini via OpenAI-style code"
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return OpenAI(api_key=openai_key), os.getenv("LLM_MODEL", "gpt-4o-mini"), "OpenAI"
    raise LLMError("No GEMINI_API_KEY found in your .env file.")


PROBE_TOOL = [{
    "type": "function",
    "function": {
        "name": "search_products",
        "description": "Search a catalog of real products.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "search words"}},
                       "required": ["query"]},
    },
}]

def run_self_test(steps):
    client, model, provider = get_client()
    steps.append(f"Using {provider}, model '{model}'")
 
    steps.append("Test 1 (plain chat): asked the AI to say hello in five words")
    r = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": "Say hello in five words."}])
    hello = (r.choices[0].message.content or "").strip()
    steps.append("The AI replied with text")
 
    steps.append("Test 2 (tool calling): asked for a gift and offered a search_products tool")
    r2 = client.chat.completions.create(
        model=model, tools=PROBE_TOOL, tool_choice="auto",
        messages=[{"role": "user", "content": "Find me a gardening gift under $50."}])
    calls = r2.choices[0].message.tool_calls
    tool = None
    if calls:
        tool = {"name": calls[0].function.name, "arguments": calls[0].function.arguments}
        steps.append("The AI chose to call our tool instead of answering. That is the core of an agent.")
    else:
        steps.append("The AI answered in text without calling the tool (we will tune the prompt on Day 2)")
    return {"hello": hello, "tool_call": tool}
