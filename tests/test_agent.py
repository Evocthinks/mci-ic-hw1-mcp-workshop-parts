"""Runs the agent loop against the real server over stdio, with a scripted model instead of an LLM."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import RateLimitError

sys.path.insert(0, str(Path(__file__).parent.parent))
import agent  # noqa: E402

ROOT = Path(__file__).parent.parent


class Message:
    """Minimal stand-in for an OpenAI chat message."""

    def __init__(self, content, calls=()):
        self.content = content
        self.tool_calls = [
            SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
            for i, (n, a) in enumerate(calls, 1)
        ] or None

    def model_dump(self, exclude_none=True):
        data = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.function.name,
                                                              "arguments": c.function.arguments}}
                for c in self.tool_calls
            ]
        return data


class ScriptedLLM:
    """Plays: search, price, log, final answer. Records what the loop sent back."""

    def __init__(self):
        self.step = 0
        self.seen_tool_messages = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.script = [
            Message("Thought: look up the part.", [("search_inventory", {"query": "6205"})]),
            Message("Thought: price for 120 pieces.",
                    [("calculate_tiered_price", {"unit_price_eur": 6.4, "quantity": 120})]),
            Message("Thought: log it.",
                    [("append_audit_event", {"event_type": "quote", "message": "BRG-6205 x 120"})]),
            Message("BRG-6205 x 120 costs 710.40 EUR."),
        ]

    async def create(self, model, messages, tools):
        self.seen_tool_messages = [m for m in messages if m["role"] == "tool"]
        self.step += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=self.script[self.step - 1])])


def test_loop_executes_tool_calls_over_mcp(tmp_path, capsys):
    env = {**os.environ, "INVENTORY_DB": str(tmp_path / "inv.db"), "AUDIT_LOG_PATH": str(tmp_path / "audit.log")}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], env=env)

    async def run():
        llm = ScriptedLLM()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                answer = await agent.ask("quote 120 pieces 6205", session, llm, "scripted")
                log = await session.read_resource("audit://log")
        return names, answer, llm, log.contents[0].text

    names, answer, llm, log_text = asyncio.run(run())
    assert names == {"search_inventory", "calculate_tiered_price", "append_audit_event"}
    assert answer == "BRG-6205 x 120 costs 710.40 EUR."
    assert "710.4" in llm.seen_tool_messages[1]["content"]
    assert "BRG-6205 x 120" in log_text
    out = capsys.readouterr().out
    assert "Action: calculate_tiered_price" in out and "Observation:" in out


def rate_limit_error(message):
    response = httpx.Response(429, request=httpx.Request("POST", "https://llm.test/chat/completions"))
    return RateLimitError(message, response=response, body=None)


def flaky_llm(errors):
    """Raises the given errors one by one, then answers."""
    async def create(model, messages, tools):
        if errors:
            raise errors.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=Message("done"))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.fixture
def waits(monkeypatch):
    seconds = []

    async def fake_sleep(delay):
        seconds.append(delay)
    monkeypatch.setattr(agent.asyncio, "sleep", fake_sleep)
    return seconds


def test_complete_waits_for_retry_delay_on_rate_limit(waits):
    llm = flaky_llm([rate_limit_error("Quota exceeded, limit: 5. Please retry in 38.1s.")])
    reply = asyncio.run(agent.complete(llm, "scripted", [], []))
    assert reply.content == "done"
    assert waits == [pytest.approx(39.1)]


def test_complete_gives_up_when_daily_quota_is_used_up(waits):
    llm = flaky_llm([rate_limit_error("Quota exceeded. Please retry in 41000s.")])
    with pytest.raises(RateLimitError):
        asyncio.run(agent.complete(llm, "scripted", [], []))
    assert waits == []
