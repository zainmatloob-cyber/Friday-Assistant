"""
memory.py — the difference between a tool and an assistant.

Everything FRIDAY learns about you lives in ~/.friday/memory.json and survives
restarts. Nothing else in this project persists; this file is why FRIDAY stops
asking you where your E drive projects are.

Design note: memory is injected into the system prompt on EVERY call, so it has
to stay small. We cap the digest and keep entries short. Memory that costs you
2,000 tokens a call is not worth having on a rate-limited tier.
"""

import json
import datetime
from pathlib import Path

MEMORY_DIR = Path.home() / ".friday"
MEMORY_FILE = MEMORY_DIR / "memory.json"

MAX_ENTRIES = 60          # oldest non-pinned entries drop off past this
MAX_DIGEST_CHARS = 1200   # ~300 tokens injected per call

CATEGORIES = ("identity", "project", "preference", "system", "general")


def _load() -> dict:
    if not MEMORY_FILE.exists():
        return {"entries": {}}
    try:
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        # corrupt file shouldn't brick the assistant
        return {"entries": {}}


def _save(data: dict) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def remember(key: str, value: str, category: str = "general",
             pinned: bool = False) -> str:
    """
    Store a durable fact. Use a short stable key so it can be overwritten later
    rather than duplicated ('user_name', 'careerlens_path', 'preferred_editor').
    """
    if category not in CATEGORIES:
        category = "general"
    data = _load()
    existed = key in data["entries"]
    data["entries"][key] = {
        "value": value,
        "category": category,
        "pinned": bool(pinned),
        "updated": datetime.datetime.now().isoformat(timespec="seconds"),
    }

    # evict oldest unpinned entries if we're over budget
    entries = data["entries"]
    if len(entries) > MAX_ENTRIES:
        unpinned = sorted(
            ((k, v) for k, v in entries.items() if not v.get("pinned")),
            key=lambda kv: kv[1].get("updated", ""))
        for k, _ in unpinned[:len(entries) - MAX_ENTRIES]:
            del entries[k]

    _save(data)
    return f"{'Updated' if existed else 'Remembered'}: {key} = {value}"


def recall(query: str = "") -> str:
    """Look up what FRIDAY knows. Blank query returns everything."""
    entries = _load()["entries"]
    if not entries:
        return "I don't know anything about you yet."
    q = query.lower().strip()
    hits = {k: v for k, v in entries.items()
            if not q or q in k.lower() or q in str(v["value"]).lower()}
    if not hits:
        return f"Nothing stored matching '{query}'."
    rows = [f"  [{v['category']}] {k}: {v['value']}" for k, v in sorted(hits.items())]
    return f"Known ({len(hits)}):\n" + "\n".join(rows)


def forget(key: str) -> str:
    data = _load()
    if key not in data["entries"]:
        return f"Nothing stored under '{key}'. Use recall to see what exists."
    value = data["entries"].pop(key)["value"]
    _save(data)
    return f"Forgotten: {key} (was: {value})"


def digest(max_chars: int = MAX_DIGEST_CHARS) -> str:
    """
    Compact block injected into the system prompt. Identity and pinned facts
    come first so they survive truncation.
    """
    entries = _load()["entries"]
    if not entries:
        return ""

    def rank(item):
        k, v = item
        return (0 if v.get("pinned") else 1,
                CATEGORIES.index(v.get("category", "general")),
                k)

    lines, total = [], 0
    for k, v in sorted(entries.items(), key=rank):
        line = f"- {k}: {v['value']}"
        if total + len(line) > max_chars:
            lines.append(f"- [{len(entries) - len(lines)} more facts — use recall]")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def stats() -> str:
    entries = _load()["entries"]
    if not entries:
        return f"Memory empty. File would be: {MEMORY_FILE}"
    by_cat: dict[str, int] = {}
    for v in entries.values():
        by_cat[v.get("category", "general")] = by_cat.get(v.get("category", "general"), 0) + 1
    breakdown = ", ".join(f"{c}:{n}" for c, n in sorted(by_cat.items()))
    return (f"{len(entries)} facts ({breakdown})\n"
            f"~{len(digest()) // 4} tokens injected per call\n"
            f"File: {MEMORY_FILE}")


def wipe() -> str:
    _save({"entries": {}})
    return "Memory wiped."
