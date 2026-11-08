#!/usr/bin/env python3
"""Thoth MCP server (stdio, JSON-RPC 2.0, no third-party dependencies).

Two modes:

* Worker mode (``--run R<n>``): started by Thoth for an agent run. Exposes only
  thoth_status, thoth_context and thoth_note, so a worker can read the brain
  and record decisions but cannot launch other agents.
* Coordinator mode (no ``--run``): registered by the user in Claude Code or
  Codex (``claude mcp add thoth -- python -I mcp_server.py --as claude``). Exposes
  every Thoth tool, so that tool can plan, assign, review and merge exactly like
  Hermes does. Which tool may do what is decided by its own permission prompts.

Usage: mcp_server.py [--project DIR] [--as claude|codex|hermes] [--run R] [--task T] [--data-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # works under python -I too

from thoth_core import __version__  # noqa: E402
from thoth_core.brain import BrainError  # noqa: E402
from thoth_core.dispatch import call  # noqa: E402
from thoth_core.schemas import HERMES_ONLY, TOOLS, WORKER_TOOLS  # noqa: E402
from thoth_core.service import Thoth  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"

_out_lock = threading.Lock()


def _send(message: dict) -> None:
    data = json.dumps(message, ensure_ascii=False)
    with _out_lock:
        sys.stdout.write(data + "\n")
        sys.stdout.flush()


def _result(req_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


class Server:
    def __init__(self, args: argparse.Namespace):
        self.project = args.project or os.getcwd()
        self.actor = args.as_agent
        self.worker = bool(args.run)
        self.run_ref = args.run or ""
        self.task_ref = args.task or ""
        self.svc = Thoth(actor=self.actor, data_dir=args.data_dir or None)  # Hermes's records of what the user approved
        self.names = WORKER_TOOLS if self.worker else tuple(n for n in TOOLS if n not in HERMES_ONLY)
        self.inflight: list[threading.Thread] = []

    def instructions(self) -> str:
        if self.worker:
            return (
                f"Thoth shared brain for this project. You are {self.actor}, working on run {self.run_ref} of task "
                f"{self.task_ref}. Record decisions other agents must follow with thoth_note(kind='decision'), "
                "and read the board with thoth_status."
            )
        return (
            "Thoth: a shared brain that lets Claude Code, Codex and Hermes work on one project as a team. "
            "As coordinator: thoth_status to see the board, thoth_plan to add tasks, thoth_assign to start an agent in its own "
            "git worktree, thoth_wait for results, thoth_review for cross-review by the other agent, thoth_merge to merge."
        )

    def tools(self) -> list[dict]:
        return [
            {"name": name, "description": TOOLS[name][0], "inputSchema": TOOLS[name][1]}
            for name in self.names
        ]

    def call_tool(self, req_id, params: dict) -> None:
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if name not in self.names:
            _result(req_id, {"content": [{"type": "text", "text": f"Unknown or unavailable tool: {name}"}], "isError": True})
            return
        if self.worker and name == "thoth_note":
            arguments.setdefault("task", self.task_ref)
            arguments["run"] = self.run_ref
        try:
            data = call(self.svc, name, arguments, lambda: self.project)
            _result(req_id, {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}], "isError": False})
        except BrainError as exc:
            _result(req_id, {"content": [{"type": "text", "text": f"Thoth: {exc}"}], "isError": True})
        except Exception as exc:  # never crash the server on one bad call
            logging.getLogger("thoth").error("tool %s failed\n%s", name, traceback.format_exc())
            _result(req_id, {"content": [{"type": "text", "text": f"Thoth internal error: {exc}"}], "isError": True})

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            version = params.get("protocolVersion") or PROTOCOL_VERSION
            _result(
                req_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "thoth", "version": __version__},
                    "instructions": self.instructions(),
                },
            )
        elif method == "tools/list":
            _result(req_id, {"tools": self.tools()})
        elif method == "tools/call":
            # Long calls (thoth_wait) must not block other requests.
            worker = threading.Thread(target=self.call_tool, args=(req_id, params), daemon=True)
            self.inflight.append(worker)
            worker.start()
        elif method == "ping":
            _result(req_id, {})
        elif req_id is not None and not str(method or "").startswith("notifications/"):
            _error(req_id, -32601, f"Method not found: {method}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Thoth MCP server")
    parser.add_argument("--project", default="")
    parser.add_argument("--as", dest="as_agent", default="claude", choices=["claude", "codex", "hermes"])
    parser.add_argument("--run", default="")
    parser.add_argument("--task", default="")
    parser.add_argument("--data-dir", dest="data_dir", default="")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="thoth-mcp %(levelname)s %(message)s")
    server = Server(args)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _error(None, -32700, "Parse error")
            continue
        if isinstance(msg, list):  # batch
            for item in msg:
                if isinstance(item, dict):
                    server.handle(item)
        elif isinstance(msg, dict):
            server.handle(msg)
    # The client closed stdin: answer the calls still in flight, then stop our agent runs.
    for worker in server.inflight:
        worker.join(timeout=30)
    server.svc.manager.shutdown("MCP client disconnected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
