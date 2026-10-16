"""LLM backends behind one small interface.

The agent talks to any backend through the same five calls:
    reset(system, tools)   start a conversation with a system prompt and tool specs
    add_user(text)         add a user message
    step() -> Turn         ask the model for its next move (text and/or tool calls)
    add_tool_results(...)  give the model the results of its tool calls

Two backends:
    AnthropicBackend         Claude via the Messages API
    OpenAICompatBackend      any OpenAI-compatible Chat Completions API:
                             Gemini, OpenAI, Groq, Ollama, ...
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

# Provider presets. Override the model with --model or LLM_MODEL, and the
# endpoint with LLM_BASE_URL.
PRESETS: dict[str, dict[str, Any]] = {
    "anthropic": {"kind": "anthropic", "model": "claude-sonnet-5-5", "key_env": "ANTHROPIC_API_KEY"},
    "gemini": {
        "kind": "openai",
        "model": "gemini-3.8-flash",
        "key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "openai": {"kind": "openai", "model": None, "key_env": "OPENAI_API_KEY", "base_url": None},
    "ollama": {"kind": "openai", "model": None, "key_env": None, "base_url": "http://localhost:11434/v1"},
}


class LLMError(RuntimeError):
    """The LLM API call failed (bad key, rate limit, network, ...)."""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ToolResult:
    id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None


# -- Claude -------------------------------------------------------------------

class AnthropicBackend:
    provider = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, max_tokens: int = 2048, http_client: Any = None):
        import anthropic

        self._anthropic = anthropic
        kwargs: dict[str, Any] = {"api_key": api_key}
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.max_tokens = max_tokens
        self.system = ""
        self.tools: list[dict] = []
        self.messages: list[dict] = []

    def reset(self, system: str, tools: list[ToolSpec]) -> None:
        self.system = system
        self.tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
        self.messages = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def step(self) -> Turn:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system,
                tools=self.tools,
                messages=self.messages,
            )
        except self._anthropic.APIError as e:
            raise LLMError(f"Claude API error: {e}") from e

        if resp.content:
            self.messages.append({"role": "assistant", "content": resp.content})
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in resp.content if b.type == "tool_use"]
        return Turn(
            text=text,
            tool_calls=calls,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason,
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # All results for one assistant turn go in a single user message.
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": r.id, "content": r.content, "is_error": r.is_error}
                    for r in results
                ],
            }
        )


# -- OpenAI-compatible (Gemini, OpenAI, Groq, Ollama, ...) ----------------------

# Keep schemas to the JSON Schema subset every compatible API accepts.
_SCHEMA_KEYS = {
    "type", "description", "properties", "required", "items", "enum",
    "minimum", "maximum", "minItems", "maxItems", "format", "nullable", "anyOf",
}


def _clean_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        out = {}
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                out[key] = {name: _clean_schema(prop) for name, prop in value.items()}
            elif key in _SCHEMA_KEYS:
                out[key] = _clean_schema(value)
        return out
    if isinstance(schema, list):
        return [_clean_schema(s) for s in schema]
    return schema


class OpenAICompatBackend:
    def __init__(
        self,
        model: str,
        api_key: str | None,
        base_url: str | None = None,
        provider: str = "openai",
        http_client: Any = None,
    ):
        import openai

        self._openai = openai
        kwargs: dict[str, Any] = {"api_key": api_key or "not-needed", "base_url": base_url}
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.client = openai.OpenAI(**kwargs)
        self.model = model
        self.provider = provider
        self.tools: list[dict] = []
        self.messages: list[dict] = []

    def reset(self, system: str, tools: list[ToolSpec]) -> None:
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": _clean_schema(t.input_schema),
                },
            }
            for t in tools
        ]
        self.messages = [{"role": "system", "content": system}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def step(self) -> Turn:
        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=self.messages, tools=self.tools, tool_choice="auto"
            )
        except self._openai.APIError as e:
            raise LLMError(f"{self.provider} API error: {e}") from e

        msg = resp.choices[0].message
        # Keep the assistant message exactly as the API returned it, including
        # provider-specific fields. Gemini 3 puts a "thought signature" in
        # tool_calls[].extra_content.google.thought_signature and rejects the
        # next request (400) if it isn't sent back.
        entry: dict[str, Any] = msg.model_dump(exclude_none=True)
        entry["role"] = "assistant"
        calls: list[ToolCall] = []
        if msg.tool_calls:
            for c in msg.tool_calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {"_invalid_json": c.function.arguments}
                calls.append(ToolCall(c.id, c.function.name, args if isinstance(args, dict) else {}))
        self.messages.append(entry)
        usage = resp.usage
        return Turn(
            text=msg.content or "",
            tool_calls=calls,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            stop_reason=resp.choices[0].finish_reason,
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            content = f"ERROR: {r.content}" if r.is_error else r.content
            self.messages.append({"role": "tool", "tool_call_id": r.id, "content": content})


# -- factory --------------------------------------------------------------------

def make_backend(provider: str | None = None, model: str | None = None):
    """Build a backend from a provider name, or pick one from the API keys present."""
    provider = provider or os.environ.get("LLM_PROVIDER")
    if not provider:
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("GEMINI_API_KEY"):
            provider = "gemini"
        else:
            raise LLMError(
                "No LLM configured. Set ANTHROPIC_API_KEY or GEMINI_API_KEY, "
                "or LLM_PROVIDER (openai/ollama) with LLM_MODEL."
            )
    if provider not in PRESETS:
        raise LLMError(f"Unknown provider '{provider}'. Choose from: {', '.join(PRESETS)}.")

    preset = PRESETS[provider]
    model = model or os.environ.get("LLM_MODEL") or preset["model"]
    if not model:
        raise LLMError(f"Provider '{provider}' needs a model: pass --model or set LLM_MODEL.")

    key_env = preset["key_env"]
    api_key = os.environ.get(key_env) if key_env else None
    if key_env and not api_key:
        raise LLMError(f"Provider '{provider}' needs an API key: set {key_env}.")

    if preset["kind"] == "anthropic":
        return AnthropicBackend(model=model, api_key=api_key)
    base_url = os.environ.get("LLM_BASE_URL") or preset.get("base_url")
    return OpenAICompatBackend(model=model, api_key=api_key, base_url=base_url, provider=provider)
