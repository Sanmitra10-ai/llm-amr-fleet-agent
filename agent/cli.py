"""Chat with the fleet agent from the terminal.

Run from the repo root, with the API server and the sim running:

    python -m agent.cli                                  # interactive
    python -m agent.cli -c "send a robot to the pantry"  # one command, then exit
    python -m agent.cli --provider gemini                # pick the LLM

In the chat: /reset starts a fresh conversation, /quit exits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.agent import FleetAgent  # noqa: E402
from agent.llm import PRESETS, LLMError, make_backend  # noqa: E402
from agent.log import SessionLogger  # noqa: E402

DIM, RED, GREEN, RESET = "\033[2m", "\033[31m", "\033[32m", "\033[0m"


def _short(value: object, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def print_event(kind: str, data: dict) -> None:
    if kind == "thinking":
        print(f"{DIM}  … {_short(data['text'])}{RESET}")
    elif kind == "tool_call":
        args = ", ".join(f"{k}={json.dumps(v)}" for k, v in data["args"].items())
        print(f"{DIM}  → {data['name']}({args}){RESET}")
    elif kind == "tool_result":
        colour, mark = (RED, "✗") if data["is_error"] else (GREEN, "✓")
        print(f"{DIM}    {colour}{mark}{RESET}{DIM} {_short(data['content'])}  [{data['latency_ms']} ms]{RESET}")
    elif kind == "progress":
        robot = data.get("robot") or "-"
        print(f"{DIM}    {time.strftime('%H:%M:%S')} {data['task_id']}: {data['status']} (robot {robot}){RESET}")


def ask_operator(question: str) -> bool:
    answer = input(f"\n  ⚠ {question} [y/N] ").strip().lower()
    return answer in ("y", "yes")


async def run(args: argparse.Namespace) -> int:
    try:
        backend = make_backend(args.provider, args.model)
    except LLMError as e:
        print(f"Setup error: {e}")
        return 1

    logger = SessionLogger(args.log_dir)
    env = {**os.environ, "RMF_MCP_LOG_LEVEL": "WARNING"}
    server = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"], cwd=str(REPO_ROOT), env=env)

    async with Client(server) as mcp:
        agent = FleetAgent(
            backend,
            mcp,
            logger,
            confirm=None if args.yes else ask_operator,
            on_event=print_event,
            max_steps=args.max_steps,
            poll_s=args.poll,
        )
        await agent.start()
        print(f"Fleet agent ready ({backend.provider} / {backend.model}). Log: {logger.path}")

        if args.command:
            print(f"\nyou> {args.command}")
            reply = await agent.ask(args.command)
            print(f"\nagent> {reply}")
            return 0

        print("Type a command for the fleet. /reset clears the conversation, /quit exits.")
        while True:
            try:
                text = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text:
                continue
            if text in ("/quit", "/exit"):
                return 0
            if text == "/reset":
                await agent.start()
                print("Conversation cleared.")
                continue
            reply = await agent.ask(text)
            print(f"\nagent> {reply}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--command", help="run one command and exit")
    parser.add_argument("--provider", choices=list(PRESETS), help="LLM provider (default: from API keys present)")
    parser.add_argument("--model", help="model name (default: provider preset or LLM_MODEL)")
    parser.add_argument("--yes", action="store_true", help="approve risky actions without asking")
    parser.add_argument("--max-steps", type=int, default=15, help="LLM calls allowed per request")
    parser.add_argument("--poll", type=float, default=10, help="seconds between task status checks while waiting")
    parser.add_argument("--log-dir", default=str(REPO_ROOT / "logs"))
    try:
        return asyncio.run(run(parser.parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
