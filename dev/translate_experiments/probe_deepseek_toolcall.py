"""Probe: what does deepseek-v4-flash emit when given a tool?
Dumps the raw response object so we can see the exact tool_calls schema."""
import json
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                    "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]

print("=" * 70)
print("TEST 1: single tool call (weather)")
print("=" * 70)
resp = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[
        {"role": "system", "content": "You are a helpful assistant with access to tools."},
        {"role": "user", "content": "What's the weather in Rabat in celsius?"},
    ],
    tools=TOOLS,
    extra_body={"thinking": {"type": "disabled"}},
)
print(resp.model_dump_json(indent=2))

print()
print("=" * 70)
print("TEST 2: parallel tool calls")
print("=" * 70)
resp2 = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[
        {"role": "system", "content": "You are a helpful assistant with access to tools."},
        {"role": "user", "content": "Check the weather in Rabat and search for best tagines."},
    ],
    tools=TOOLS,
    extra_body={"thinking": {"type": "disabled"}},
)
print(resp2.model_dump_json(indent=2))

print()
print("=" * 70)
print("TEST 3: full round trip — send tool result back")
print("=" * 70)
assistant_msg = resp.choices[0].message
tool_call = assistant_msg.tool_calls[0]
followup = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[
        {"role": "system", "content": "You are a helpful assistant with access to tools."},
        {"role": "user", "content": "What's the weather in Rabat in celsius?"},
        assistant_msg.model_dump(exclude_none=True),
        {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps({"city": "Rabat", "temp_c": 22, "conditions": "sunny"}),
        },
    ],
    tools=TOOLS,
    extra_body={"thinking": {"type": "disabled"}},
)
print(followup.model_dump_json(indent=2))
