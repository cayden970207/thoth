"""What each version of Thoth brings, in the words of CHANGELOG.md, and which of it this user has seen.

CHANGELOG.md is the one place a release is described. After an update, the Thoth page shows the notes the
user has not seen yet, once; each GitHub release uses the same words:

    python3 -m thoth_core.changelog 0.9.0
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
SEEN = "whatsnew.json"  # in Thoth's data folder: the newest version whose notes this user has seen
# Thoth was in use before it kept SEEN: an update, not a new install.
_USED = ("projects.json", "chats.json")
_HEADING = re.compile(r"^##\s+\[?(\d+\.\d+\.\d+)\]?\s*(?:[—–-]\s*(\S.*?))?\s*$")


def key(version: object) -> tuple[int, ...]:
    """A version as numbers, so 0.10.0 comes after 0.9.0. Anything that is not x.y.z sorts first."""
    try:
        return tuple(int(part) for part in str(version).split("."))
    except ValueError:
        return ()


def entries(text: str) -> list[dict]:
    """The versions a changelog describes, in its order: {version, date, notes}."""
    out: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            current = {"version": heading.group(1), "date": heading.group(2) or "", "notes": []}
            out.append(current)
        elif line.startswith("#"):
            current = None
        elif current is None:
            continue
        elif line.startswith("- "):
            current["notes"].append(line[2:].strip())
        elif line.startswith("  ") and line.strip() and current["notes"]:
            current["notes"][-1] += " " + line.strip()  # a note that wraps onto the next line
    return out


def load(path: Path | str | None = None) -> list[dict]:
    try:
        return entries(Path(path or FILE).read_text(encoding="utf-8"))
    except OSError:
        return []


def _seen(data_dir: Path | str) -> str:
    try:
        return str(json.loads((Path(data_dir) / SEEN).read_text(encoding="utf-8")).get("seen") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def mark_seen(data_dir: Path | str, version: str) -> None:
    """Remember that this user has seen the notes up to this version."""
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (SEEN + ".tmp")
    tmp.write_text(json.dumps({"seen": version}) + "\n", encoding="utf-8")
    tmp.replace(folder / SEEN)


def start(data_dir: Path | str, current: str) -> str:
    """The version whose notes this user saw last ('' when that is unknown). The first time this runs, a new
    install starts at the current version, so a first project never opens with a list of changes; someone
    who used Thoth before it said what's new gets this version's notes."""
    seen = _seen(data_dir)
    if seen:
        return seen
    if any((Path(data_dir) / name).exists() for name in _USED):
        return ""
    mark_seen(data_dir, current)
    return current


def whats_new(data_dir: Path | str, current: str, path: Path | str | None = None) -> dict:
    """Every version's notes, newest first, and the ones this user has not seen yet."""
    versions = sorted(load(path), key=lambda e: key(e["version"]), reverse=True)
    seen = start(data_dir, current)
    if seen:
        unseen = [e for e in versions if key(seen) < key(e["version"]) <= key(current)]
    else:
        unseen = [e for e in versions if e["version"] == current]
    return {"version": current, "seen": seen, "unseen": unseen, "versions": versions}


def release_notes(version: str, path: Path | str | None = None) -> str:
    """One version's notes as Markdown, for its GitHub release."""
    for entry in load(path):
        if entry["version"] == version:
            return "\n".join("- " + note for note in entry["notes"])
    raise KeyError(f"CHANGELOG.md has no section for {version}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 -m thoth_core.changelog <version>")
    try:
        print(release_notes(sys.argv[1]))
    except KeyError as exc:
        sys.exit(str(exc.args[0]))
