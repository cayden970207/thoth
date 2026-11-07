"""A web browser for an agent: Chrome DevTools MCP, in a clean Chrome or in the user's own.

A task can give the agent that builds it a browser (the task's ``browser``):

* ``clean``: a headless Chrome with a temporary profile that is deleted when the run ends.
  No logins and no history. Enough to look at public websites.
* ``user``: the user's own running Chrome (version 144 or newer), with their logins. The user
  first turns on remote debugging at chrome://inspect/#remote-debugging, and Chrome asks them to
  allow each new connection (one per run). The agent opens its own tab and is told to work only
  there, to only look, and to close the tab when it is done. It can still see the titles and
  addresses of the other open tabs, which the approval card says.

Either way the agent reaches the browser through Google's Chrome DevTools MCP server, pinned to
one exact version and fetched by npx on first use. Usage statistics, CrUX lookups and sensitive
request headers are switched off. The agent's shell commands still have no network: only the browser does.
Reviews never get a browser.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from .agents import BROWSER_SERVER, McpSpec
from .brain import BrainError

PACKAGE = "chrome-devtools-mcp@1.2.0"  # no dependencies of its own, so this pins everything npx fetches
MODES = ("clean", "user")
LABEL = {"": "Off", "clean": "Clean Chrome", "user": "Your Chrome"}

_COMMON = ["--no-usage-statistics", "--no-performance-crux", "--redactNetworkHeaders"]
_FLAGS = {
    "clean": ["--headless", "--isolated", "--viewport", "1440x900"],
    "user": ["--autoConnect"],
}
_WORDS = {
    "": ("", "off", "none", "no", "false", "0", "disabled"),
    "clean": ("clean", "clean chrome", "fresh", "headless", "isolated", "on", "yes", "true", "1"),
    "user": ("user", "user chrome", "users chrome", "your chrome", "yours", "mine", "my chrome", "own", "logged in"),
}

# What the approval card says, in plain words.
CARD = {
    "clean": "It can open websites in a fresh Chrome without your logins; that Chrome is deleted when it finishes.",
    "user": ("It will use your own Chrome: it opens its own tab, can see the titles of your other tabs, and acts with your logins. "
             "It is told to only look: no posting, buying or changing settings. "
             "When it starts browsing, Chrome asks you to allow the connection: click Allow."),
}


def normalize(value: object) -> str:
    """'' (no browser), 'clean' or 'user', from whatever a model or a person wrote."""
    text = " ".join(str(value or "").lower().replace("'", "").replace("_", " ").replace("-", " ").split())
    for mode, words in _WORDS.items():
        if text in words:
            return mode
    raise BrainError("browser must be off, clean (a fresh Chrome without logins) or user (your own Chrome)")


def npx() -> str | None:
    """npx on PATH, else the Node.js that Hermes ships with."""
    found = shutil.which("npx")
    if found:
        return found
    bundled = Path.home() / ".hermes" / "node" / "bin" / "npx"
    return str(bundled) if bundled.exists() else None


def node_dir(npx_path: str) -> str:
    """The folder holding the node that this npx belongs to, following its links ("" if unknown)."""
    p = Path(npx_path)
    for _ in range(6):
        if (p.parent / "node").exists():
            return str(p.parent)
        if not p.is_symlink():
            break
        p = p.parent / os.readlink(p)
    return ""


_NODE_CHECKED: dict[str, tuple[bool, str]] = {}


def _node_ok(folder: str) -> tuple[bool, str]:
    node = os.path.join(folder, "node") if folder else (shutil.which("node") or "")
    if not node:
        return False, "Web browsing needs Node.js 20.19 or newer, and none was found. Install Node.js, or turn the browser off for this task."
    if node in _NODE_CHECKED:
        return _NODE_CHECKED[node]
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL).stdout.strip()
        major, minor = (int(x) for x in out.lstrip("v").split(".")[:2])
        ok = major >= 23 or (major == 22 and minor >= 12) or (major == 20 and minor >= 19)
        result = (ok, "" if ok else f"Web browsing needs Node.js 20.19, 22.12 or newer; {node} is {out}.")
    except Exception as exc:
        result = (False, f"Web browsing needs Node.js 20.19 or newer; could not run {node}: {exc}")
    _NODE_CHECKED[node] = result
    return result


def chrome_installed() -> bool:
    if sys.platform == "darwin":
        return any((Path(base) / "Google Chrome.app").exists() for base in ("/Applications", Path.home() / "Applications"))
    return any(shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chrome"))


def devtools_port_file() -> Path:
    """Chrome writes the port of its remote-debugging server here while the switch is on."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "DevToolsActivePort"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "google-chrome" / "DevToolsActivePort"


def user_chrome_open() -> bool:
    """True when the user's Chrome is running with remote debugging turned on (nothing connects to it)."""
    try:
        port = int(devtools_port_file().read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    try:
        socket.create_connection(("127.0.0.1", port), 0.5).close()
        return True
    except OSError:
        return False


def ready(mode: str) -> tuple[bool, str]:
    """Whether a run with this browser can start, and what the user should do if not."""
    if not mode:
        return True, ""
    found = npx()
    if not found:
        return False, "Web browsing needs Node.js (npx), and none was found. Install Node.js 20.19 or newer, or turn the browser off for this task."
    ok, why = _node_ok(node_dir(found))
    if not ok:
        return False, why
    if not chrome_installed():
        return False, "Web browsing needs Google Chrome. Install it, or turn the browser off for this task."
    if mode == "user" and not user_chrome_open():
        return False, ("Your Chrome is not open to agents yet. In Chrome, open chrome://inspect/#remote-debugging and turn on "
                       "remote debugging (Chrome 144 or newer), then try again. Or give this task a clean Chrome instead.")
    return True, ""


def spec(mode: str) -> McpSpec:
    """How the agent starts the browser server."""
    return McpSpec(command=npx() or "npx", args=["-y", PACKAGE, *_FLAGS[mode], *_COMMON])


def with_node(env: dict[str, str]) -> dict[str, str]:
    """npx runs `node` from PATH: make sure that is the node next to it (e.g. Hermes's own)."""
    found = npx()
    folder = node_dir(found) if found else ""
    parts = env.get("PATH", "").split(os.pathsep)
    if folder and folder not in parts:
        env = {**env, "PATH": os.pathsep.join([folder, *[p for p in parts if p]])}
    return env


_SHOTS = ("- These tools click, type, drag with the mouse and press keys one press at a time. They have no touch input and can't "
          "hold a key down: test those paths as far as that allows and list the rest under `limits`.\n"
          "- Screenshots: capture what is on screen, not the full page, unless the task needs the whole page. Save the ones that "
          "belong in your result as .jpg (format jpeg) inside your worktree and list them under outputs.\n"
          f"- Use only these `{BROWSER_SERVER}` tools for the web, not a browser skill or command you may know of.\n")


def brief(mode: str) -> str:
    """The section of the agent's brief that explains its browser."""
    if mode == "clean":
        return (
            "## Web browser\n"
            f"The `{BROWSER_SERVER}` tools drive a fresh headless Chrome with no logins; it is deleted when you finish.\n"
            "- Use it for the websites this task needs: new_page or navigate_page to open a page, take_snapshot to read it, "
            "evaluate_script for exact colors and fonts, take_screenshot to capture it.\n"
            "- Your shell has no network. Reach the web only through these tools.\n" + _SHOTS
        )
    if mode == "user":
        return (
            "## Web browser: the user's own Chrome\n"
            f"The `{BROWSER_SERVER}` tools are connected to the user's everyday Chrome, with their logins and their own tabs open.\n"
            "- Open your own tab with new_page and work only in it. Never select, read, navigate, screenshot or close another tab, "
            "and never repeat other tabs' titles or addresses.\n"
            "- Only look. Do not submit forms, post, send, buy, delete, or change any account or setting, unless the task asks for "
            "that exact action. If the task needs one, stop and say so in your report.\n"
            "- Close your tab with close_page before you finish.\n"
            "- Chrome asks the user to allow your connection when you first use these tools. If a tool says it could not "
            "connect to Chrome, the user has not clicked Allow yet: run `sleep 30`, then try once more. If it fails again, stop "
            "and report that the user needs to click Allow in Chrome's prompt.\n"
            "- Your shell has no network. Reach the web only through these tools.\n" + _SHOTS
        )
    return ""
