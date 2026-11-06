"""Read-only local previews of what a task built (a web page, a browser game).

Agents cannot open a browser inside their sandbox, and people who do not code
have no idea where a task's files live. Thoth serves a task's worktree (or the
main checkout) on 127.0.0.1 so anyone can open it with one click. The server
only reads files, never lists hidden files, and stops serving a folder when
the list of open previews grows past ``MAX_SERVERS``.
"""

from __future__ import annotations

import functools
import http.server
import os
import re
import threading
import urllib.parse
from pathlib import Path

MAX_SERVERS = 8
SKIP_DIRS = {".git", ".thoth", "node_modules", ".venv", "venv", "__pycache__"}

_servers: dict[str, tuple[http.server.ThreadingHTTPServer, int]] = {}
_lock = threading.Lock()


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Static files only. Hidden paths (.env, .git, …) answer 404."""

    def log_message(self, *_args) -> None:  # keep the host's log clean
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self):
        self._range_left = None
        path = urllib.parse.unquote(self.path.split("?", 1)[0].split("#", 1)[0])
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if any(p.startswith(".") for p in parts):
            self.send_error(404)
            return None
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        local = self.translate_path(self.path)
        if not rng or not os.path.isfile(local) or not (rng.group(1) or rng.group(2)):
            return super().send_head()
        # Byte ranges, so video and sound can load their metadata and seek.
        f = open(local, "rb")
        size = os.fstat(f.fileno()).st_size
        if rng.group(1):
            start, end = int(rng.group(1)), int(rng.group(2)) if rng.group(2) else size - 1
        else:
            start, end = max(0, size - int(rng.group(2))), size - 1
        end = min(end, size - 1)
        if start >= size or start > end:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(local))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        f.seek(start)
        self._range_left = end - start + 1
        return f

    def copyfile(self, source, outputfile) -> None:
        left = getattr(self, "_range_left", None)
        if left is None:
            return super().copyfile(source, outputfile)
        while left > 0:
            chunk = source.read(min(64 * 1024, left))
            if not chunk:
                break
            outputfile.write(chunk)
            left -= len(chunk)
        self._range_left = None

    def do_POST(self) -> None:  # noqa: N802 - read-only
        self.send_error(405)

    do_PUT = do_DELETE = do_PATCH = do_POST


def serve(directory: Path) -> int:
    """Serve ``directory`` on a free localhost port (reused while it stays open) and return the port."""
    key = str(Path(directory).resolve())
    with _lock:
        hit = _servers.get(key)
        if hit is not None:
            return hit[1]
        if len(_servers) >= MAX_SERVERS:
            oldest = next(iter(_servers))
            server, _ = _servers.pop(oldest)
            server.shutdown()
            server.server_close()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=key))
        server.daemon_threads = True
        port = int(server.server_address[1])
        threading.Thread(target=server.serve_forever, name=f"thoth-preview-{port}", daemon=True).start()
        _servers[key] = (server, port)
        return port


def entry_page(directory: Path, candidates: list[str] | None = None) -> str:
    """The page to open: the root index.html, else an HTML file the task produced, else the first index.html below."""
    directory = Path(directory)
    if (directory / "index.html").is_file():
        return ""
    for c in candidates or []:
        c = str(c).strip().lstrip("/")
        if c.lower().endswith((".html", ".htm")) and (directory / c).is_file():
            return c
    found = []
    for p in directory.rglob("*.html"):
        rel = p.relative_to(directory)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        found.append(rel)
    found.sort(key=lambda r: (r.name != "index.html", len(r.parts), r.as_posix()))
    return found[0].as_posix() if found else ""


def stop_all() -> None:
    with _lock:
        for server, _ in _servers.values():
            server.shutdown()
            server.server_close()
        _servers.clear()
