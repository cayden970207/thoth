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


# -- running apps -------------------------------------------------------------
#
# A full-stack app (a backend that serves its own frontend) cannot be previewed
# as static files. With a project ``run`` command, Thoth starts the app on a free
# port in a clean export of the version being looked at, under .thoth/previews/,
# so whatever the app writes (a database, logs) never lands in an agent's work.

import atexit  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402
import io  # noqa: E402
import time  # noqa: E402

from .gitops import SAFE_CONFIG  # noqa: E402

MAX_APPS = 4
_apps: dict[str, tuple[subprocess.Popen, int, str]] = {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


def run_app(repo: Path, commit: str, command: str, key: str, env: dict[str, str], previews_dir: Path,
            link_from: Path | None = None, links: tuple[str, ...] | list[str] = (), timeout: float = 25.0) -> int:
    """Start ``command`` (``{port}`` is replaced, ``PORT`` is set) in an export of ``commit``. Returns the port.

    The same key and commit reuse the running app; a newer commit restarts it.
    """
    _sweep_orphans(previews_dir)
    with _lock:
        running = _apps.get(key)
        if running is not None:
            proc, port, sha = running
            if sha == commit and proc.poll() is None:
                return port
            _stop(proc)
            _apps.pop(key, None)
        if len(_apps) >= MAX_APPS:
            oldest = next(iter(_apps))
            _stop(_apps.pop(oldest)[0])
    folder = previews_dir / f"{re.sub(r'[^A-Za-z0-9_-]+', '-', key)}-{commit[:10]}"
    if not folder.exists():
        data = subprocess.run(["git", *SAFE_CONFIG, "archive", commit], cwd=str(repo), capture_output=True, timeout=120,
                              stdin=subprocess.DEVNULL).stdout
        if not data:
            raise RuntimeError(f"Could not export {commit[:7]} to run it")
        tmp = folder.with_name(folder.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        tarfile.open(fileobj=io.BytesIO(data)).extractall(tmp, filter="data")
        for own in (".thoth-run.pid", ".thoth-run.log"):  # Thoth's own files, never ones that came with the work
            path = tmp / own
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        tmp.rename(folder)
        for rel in links:  # dependency folders the main checkout already has (node_modules, .venv)
            src, dst = (link_from / rel) if link_from else None, folder / rel
            if src is not None and src.is_dir() and dst.parent.is_dir() and not dst.exists():
                dst.symlink_to(src, target_is_directory=True)
    port = _free_port()
    log = open(folder / ".thoth-run.log", "wb")
    proc = subprocess.Popen(command.replace("{port}", str(port)), shell=True, cwd=str(folder), env={**env, "PORT": str(port)},
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            log.close()
            tail = (folder / ".thoth-run.log").read_text(errors="replace").strip()[-400:]
            raise RuntimeError(f"`{command}` stopped right away: {tail or 'no output'}")
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            break
        except OSError:
            time.sleep(0.25)
    else:
        _stop(proc)
        raise RuntimeError(f"`{command}` did not start listening on port {port} within {int(timeout)}s")
    (folder / ".thoth-run.pid").write_text(str(proc.pid))
    with _lock:
        _apps[key] = (proc, port, commit)
    return port


def _sweep_orphans(previews_dir: Path) -> None:
    """Stop apps an earlier Thoth process started and never stopped (their pidfiles outlive them)."""
    with _lock:
        mine = {proc.pid for proc, _, _ in _apps.values()}
    for pidfile in previews_dir.glob("*/.thoth-run.pid") if previews_dir.is_dir() else []:
        try:
            pid = int(pidfile.read_text().strip() or 0)
        except (OSError, ValueError):
            continue
        if pid and pid not in mine:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                pass
        pidfile.unlink(missing_ok=True)


def stop_apps() -> None:
    with _lock:
        for proc, _, _ in _apps.values():
            _stop(proc)
        _apps.clear()


# Every copy of this module (the Hermes plugin and the page's backend load their own) cleans up after itself.
atexit.register(stop_apps)
atexit.register(stop_all)
