"""Background execution of agent runs.

A run is one CLI process (``claude -p`` or ``codex exec``) in its task worktree.
The runner streams its JSONL output into ``.thoth/runs/R<n>.jsonl``, keeps the
run row's progress current for anyone watching the board, enforces a timeout,
and kills the whole process group on cancel or shutdown.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agents import Driver, RunState

logger = logging.getLogger(__name__)

PROGRESS_FLUSH_SECONDS = 1.5
KILL_GRACE_SECONDS = 5.0


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def kill_group(pid: int, grace: float = KILL_GRACE_SECONDS) -> None:
    """SIGTERM the process group, then SIGKILL whatever is left."""
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


class ActiveRun:
    def __init__(self, run_id: int):
        self.run_id = run_id
        self.proc: subprocess.Popen | None = None
        self.cancel_reason = ""
        self.timed_out = False
        self.done = threading.Event()


class RunManager:
    """Owns the processes started by this host process."""

    def __init__(self) -> None:
        self._active: dict[tuple[str, int], ActiveRun] = {}
        self._lock = threading.Lock()

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for a in self._active.values() if not a.done.is_set())

    def start(
        self,
        *,
        key: str,
        run_id: int,
        driver: Driver,
        cmd: list[str],
        prompt: str,
        cwd: Path,
        env: dict[str, str],
        log_path: Path,
        timeout: int,
        on_progress: Callable[[RunState, dict[str, Any]], None],
        on_exit: Callable[[RunState, int | None, str], None],
    ) -> ActiveRun:
        """Start the process and return at once; ``on_exit`` runs on the worker thread."""
        active = ActiveRun(run_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path = log_path.with_suffix(".stderr.log")
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_path.open("wb"),
            start_new_session=True,  # own process group, so cancel kills the whole tree
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        active.proc = proc
        with self._lock:
            self._active[(key, run_id)] = active

        def feed_prompt() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        threading.Thread(target=feed_prompt, name=f"thoth-stdin-R{run_id}", daemon=True).start()

        def watchdog() -> None:
            if not active.done.wait(timeout):
                active.timed_out = True
                active.cancel_reason = f"timed out after {timeout}s"
                kill_group(proc.pid)

        threading.Thread(target=watchdog, name=f"thoth-watchdog-R{run_id}", daemon=True).start()

        def pump() -> None:
            state = RunState()
            last_flush = 0.0
            exit_code: int | None = None
            error = ""
            try:
                on_progress(state, {"pid": proc.pid, "status": "running", "started_at": time.time()})
                assert proc.stdout is not None
                with log_path.open("a", encoding="utf-8") as log:
                    for line in proc.stdout:
                        log.write(line)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            driver.parse(line, state)
                        except Exception:  # a parser bug must not kill the run
                            logger.debug("thoth: parse failed", exc_info=True)
                        now = time.time()
                        if now - last_flush >= PROGRESS_FLUSH_SECONDS:
                            last_flush = now
                            log.flush()
                            on_progress(state, {})
                exit_code = proc.wait()
            except Exception as exc:  # never leave a run stuck in "running"
                logger.exception("thoth: run R%s pump failed", run_id)
                error = f"runner error: {exc}"
                try:
                    kill_group(proc.pid, grace=1.0)
                except Exception:
                    pass
            finally:
                if active.cancel_reason and not error:
                    error = active.cancel_reason
                if not state.error and exit_code not in (0, None):
                    tail = _tail(stderr_path)
                    if tail:
                        state.error = tail
                try:
                    on_exit(state, exit_code, error)
                except Exception:
                    logger.exception("thoth: finalizing R%s failed", run_id)
                active.done.set()

        threading.Thread(target=pump, name=f"thoth-run-R{run_id}", daemon=True).start()
        return active

    def get(self, key: str, run_id: int) -> ActiveRun | None:
        with self._lock:
            return self._active.get((key, run_id))

    def cancel(self, key: str, run_id: int, reason: str) -> bool:
        active = self.get(key, run_id)
        if active is None or active.proc is None or active.done.is_set():
            return False
        active.cancel_reason = reason
        kill_group(active.proc.pid)
        return True

    def shutdown(self, reason: str = "host shutting down") -> None:
        with self._lock:
            actives = [a for a in self._active.values() if not a.done.is_set()]
        for active in actives:
            active.cancel_reason = reason
            if active.proc is not None:
                kill_group(active.proc.pid, grace=2.0)
        for active in actives:
            active.done.wait(10)


def _tail(path: Path, limit: int = 600) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [l for l in data.strip().splitlines() if l.strip()]
    text = "\n".join(lines[-6:])
    return text[-limit:]
