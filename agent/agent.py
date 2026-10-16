"""The fleet agent: an observe -> act -> verify loop over MCP tools.

The LLM proposes tool calls; this module executes them through the MCP client,
asks the operator to approve risky actions, and logs every step.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

from mcp import Client

from agent.llm import LLMError, ToolCall, ToolResult, ToolSpec
from agent.log import SessionLogger
from agent.prompts import SYSTEM_PROMPT

DISPATCH_TOOLS = {"dispatch_patrol", "dispatch_delivery"}
ALWAYS_CONFIRM = {"cancel_task"}

# A tool implemented in the agent rather than the MCP server: it polls task
# status so the LLM doesn't spend a model call on every status check.
WAIT_TOOL = ToolSpec(
    name="wait_for_tasks",
    description=(
        "Wait until the given tasks finish (completed, failed, canceled, ...) or the timeout "
        "passes, then return each task's final status and robot. Use this after dispatching "
        "to verify the outcome. Tasks typically take 1-5 minutes in simulation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 6,
                "description": "Task IDs returned by dispatch tools",
            },
            "timeout_s": {
                "type": "integer",
                "minimum": 10,
                "maximum": 1800,
                "description": "Maximum seconds to wait (default 600)",
            },
        },
        "required": ["task_ids"],
    },
)

ConfirmFn = Callable[[str], bool]
EventFn = Callable[[str, dict], None]


class FleetAgent:
    def __init__(
        self,
        backend: Any,
        mcp: Client,
        logger: SessionLogger,
        confirm: ConfirmFn | None = None,
        on_event: EventFn | None = None,
        max_steps: int = 15,
        free_dispatches: int = 2,
        poll_s: float = 10,
    ) -> None:
        self.backend = backend
        self.mcp = mcp
        self.logger = logger
        self.confirm = confirm  # None = approve everything (used for unattended evaluation)
        self.on_event = on_event or (lambda kind, data: None)
        self.max_steps = max_steps
        self.free_dispatches = free_dispatches  # dispatches per request allowed without approval
        self.poll_s = poll_s
        self.mcp_tools: set[str] = set()
        self._dispatches_this_request = 0

    # -- setup ---------------------------------------------------------------------

    async def start(self) -> None:
        tools = (await self.mcp.list_tools()).tools
        self.mcp_tools = {t.name for t in tools}
        specs = [ToolSpec(t.name, t.description or "", t.input_schema) for t in tools] + [WAIT_TOOL]
        system = SYSTEM_PROMPT
        server_notes = getattr(self.mcp, "instructions", None)
        if server_notes:
            system += "\nNotes from the tool server:\n" + server_notes
        self.backend.reset(system, specs)
        self.logger.log(
            "session_start",
            provider=getattr(self.backend, "provider", "?"),
            model=getattr(self.backend, "model", "?"),
            tools=sorted(s.name for s in specs),
        )

    # -- main loop -------------------------------------------------------------------

    async def ask(self, text: str) -> str:
        """Handle one operator request and return the agent's final reply."""
        started = time.monotonic()
        self._dispatches_this_request = 0
        self.logger.log("user", text=text)
        self.backend.add_user(text)

        tokens_in = tokens_out = tool_calls = 0
        for step in range(1, self.max_steps + 1):
            t0 = time.monotonic()
            try:
                turn = await asyncio.to_thread(self.backend.step)
            except LLMError as e:
                self.logger.log("error", where="llm", step=step, message=str(e))
                return f"[LLM error] {e}"
            tokens_in += turn.input_tokens
            tokens_out += turn.output_tokens
            self.logger.log(
                "llm_call",
                step=step,
                latency_ms=round((time.monotonic() - t0) * 1000),
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                stop_reason=turn.stop_reason,
                text=turn.text,
                tool_calls=[{"name": c.name, "args": c.args} for c in turn.tool_calls],
            )
            if turn.text and turn.tool_calls:
                self.on_event("thinking", {"text": turn.text})

            if not turn.tool_calls:
                self._log_final(turn.text, step, started, tokens_in, tokens_out, tool_calls)
                return turn.text

            results = []
            for call in turn.tool_calls:
                results.append(await self._execute(call))
                tool_calls += 1
            self.backend.add_tool_results(results)

        reply = "I stopped because I reached the step limit for one request. Check list_tasks for what was dispatched."
        self._log_final(reply, self.max_steps, started, tokens_in, tokens_out, tool_calls, hit_step_limit=True)
        return reply

    def _log_final(self, text, steps, started, tokens_in, tokens_out, tool_calls, hit_step_limit=False):
        self.logger.log(
            "final",
            text=text,
            steps=steps,
            tool_calls=tool_calls,
            dispatches=self._dispatches_this_request,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            total_latency_ms=round((time.monotonic() - started) * 1000),
            hit_step_limit=hit_step_limit,
        )

    # -- tool execution --------------------------------------------------------------

    async def _execute(self, call: ToolCall) -> ToolResult:
        self.on_event("tool_call", {"name": call.name, "args": call.args})
        t0 = time.monotonic()
        approved: bool | None = None

        reason = self._needs_approval(call)
        if reason:
            approved = await self._ask_approval(reason)
            if not approved:
                result = ToolResult(call.id, call.name, "The operator declined this action. Do not retry it.", True)
                self._log_tool(call, result, t0, approved)
                return result

        if call.name == WAIT_TOOL.name:
            is_error, content = await self._wait_for_tasks(call.args)
        elif call.name in self.mcp_tools:
            is_error, content = await self._call_mcp(call.name, call.args)
        else:
            is_error, content = True, f"Unknown tool '{call.name}'."

        if not is_error and call.name in DISPATCH_TOOLS:
            self._dispatches_this_request += 1

        result = ToolResult(call.id, call.name, content, is_error)
        self._log_tool(call, result, t0, approved)
        return result

    def _needs_approval(self, call: ToolCall) -> str | None:
        if self.confirm is None:
            return None
        if call.name in ALWAYS_CONFIRM:
            return f"Cancel task {call.args.get('task_id')}?"
        if call.name in DISPATCH_TOOLS and self._dispatches_this_request >= self.free_dispatches:
            n = self._dispatches_this_request + 1
            return f"Dispatch #{n} for this request: {call.name} {json.dumps(call.args)}. Allow?"
        return None

    async def _ask_approval(self, question: str) -> bool:
        assert self.confirm is not None
        return await asyncio.to_thread(self.confirm, question)

    def _log_tool(self, call: ToolCall, result: ToolResult, t0: float, approved: bool | None) -> None:
        latency = round((time.monotonic() - t0) * 1000)
        self.on_event("tool_result", {"name": call.name, "is_error": result.is_error,
                                      "content": result.content, "latency_ms": latency})
        self.logger.log(
            "tool_call",
            name=call.name,
            args=call.args,
            is_error=result.is_error,
            approved=approved,
            latency_ms=latency,
            result=result.content,
        )

    async def _call_mcp(self, name: str, args: dict) -> tuple[bool, str]:
        try:
            res = await self.mcp.call_tool(name, args)
        except Exception as e:  # protocol-level failure
            return True, f"Tool call failed: {e}"
        text = "\n".join(getattr(c, "text", "") for c in res.content) if res.content else ""
        return bool(res.is_error), text

    async def _wait_for_tasks(self, args: dict) -> tuple[bool, str]:
        task_ids = [str(t) for t in (args.get("task_ids") or [])][:6]
        if not task_ids:
            return True, "Provide at least one task_id."
        timeout = min(max(int(args.get("timeout_s") or 600), 10), 1800)
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        latest: dict[str, dict] = {}

        while True:
            for tid in task_ids:
                if latest.get(tid, {}).get("finished"):
                    continue
                is_error, text = await self._call_mcp("get_task_status", {"task_id": tid})
                if is_error:
                    status = {"task_id": tid, "status": "unknown", "finished": True, "error": text}
                else:
                    status = json.loads(text)
                if status.get("status") != latest.get(tid, {}).get("status"):
                    self.on_event("progress", {"task_id": tid, "status": status.get("status"),
                                               "robot": status.get("robot")})
                latest[tid] = status
            all_done = all(s.get("finished") for s in latest.values())
            if all_done or time.monotonic() >= deadline:
                break
            await asyncio.sleep(self.poll_s)

        summary = {
            "all_finished": all_done,
            "timed_out": not all_done,
            "waited_s": round(time.monotonic() - started),
            "tasks": [
                {k: latest[t].get(k) for k in ("task_id", "status", "robot", "category", "error") if latest[t].get(k)}
                for t in task_ids
            ],
        }
        return False, json.dumps(summary)
