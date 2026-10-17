"""ReAct agent: an OpenAI-compatible LLM (LiteLLM proxy, Gemini, Azure) uses the tools of server.py over MCP.

Usage: python agent.py            runs the three demo questions
       python agent.py "question"  runs one question
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI, RateLimitError

SYSTEM_PROMPT = (
    "You are the quoting assistant of a maintenance workshop. Work in a ReAct loop: "
    "before every tool call write one short sentence starting with 'Thought:'. "
    "Use search_inventory for stock and prices, calculate_tiered_price for every price with a quantity "
    "(never calculate prices yourself), and call append_audit_event once per customer request with "
    "event_type 'quote' or 'stock_check' and a message containing SKU, quantity and result. "
    "Finish with a short answer that states the SKU, quantity, stock situation and total price."
)

QUESTIONS = [
    "How many inductive proximity sensors M12 do we have in stock and where are they stored?",
    "A customer wants 120 pieces of the cheapest 6205 bearing. What is the total price after volume discount?",
    "Can we deliver 300 pieces of the M12 sensor cable and 8 PLC input modules? Give me one price per item "
    "and log the request.",
]

MAX_STEPS = 8
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_MAX_WAIT = 90  # seconds; a longer retry delay means the daily quota is used up


def to_openai_tool(tool) -> dict:
    return {"type": "function", "function": {
        "name": tool.name, "description": tool.description or "", "parameters": tool.inputSchema}}


def retry_delay(err: RateLimitError) -> float:
    """Seconds to wait, taken from Gemini's 'Please retry in 38.1s.' or 30 s if the error does not say."""
    match = re.search(r"retry in ([\d.]+)s", str(err))
    return float(match.group(1)) if match else 30.0


async def complete(llm, model: str, messages: list, tools: list):
    """One chat completion. On HTTP 429 (requests per minute exceeded) it waits and tries again."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return (await llm.chat.completions.create(model=model, messages=messages, tools=tools)).choices[0].message
        except RateLimitError as err:
            delay = retry_delay(err) + 1
            if attempt == RATE_LIMIT_RETRIES or delay > RATE_LIMIT_MAX_WAIT:
                raise
            print(f"Rate limit (HTTP 429), waiting {delay:.0f} s ...")
            await asyncio.sleep(delay)


async def ask(question: str, session: ClientSession, llm, model: str) -> str:
    tools = [to_openai_tool(t) for t in (await session.list_tools()).tools]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]
    print(f"\nQuestion: {question}")
    for _ in range(MAX_STEPS):
        reply = await complete(llm, model, messages, tools)
        messages.append(reply.model_dump(exclude_none=True))
        if reply.content:
            print(reply.content.strip() if reply.tool_calls else f"Final answer: {reply.content.strip()}")
        if not reply.tool_calls:
            return reply.content or ""
        for call in reply.tool_calls:
            args = json.loads(call.function.arguments or "{}")
            print(f"Action: {call.function.name}({json.dumps(args)})")
            result = await session.call_tool(call.function.name, args)
            text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            print(f"Observation: {'ERROR ' if result.isError else ''}{text}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
    return "Stopped: step limit reached."


async def main(questions: list[str]) -> None:
    load_dotenv(Path(__file__).parent / ".env")
    llm = AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"])
    model = os.environ["LLM_MODEL"]
    params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).parent / "server.py")],
                                   env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("Tools:", ", ".join(t.name for t in (await session.list_tools()).tools))
            for q in questions:
                await ask(q, session, llm, model)
            log = await session.read_resource("audit://log")
            print("\nResource audit://log (last 5 lines):")
            print("\n".join(log.contents[0].text.splitlines()[-5:]))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or QUESTIONS))
