#!/usr/bin/env python3
"""Helper for the /telegram-notes skill.

  fetch  — get new text messages from the bot (getUpdates), save them to
           notes/.telegram_pending.json and print them as JSON.
  save   — read classifications from stdin, append to notes/tasks.md and
           notes/ideas.md, then (and only then) advance notes/.telegram_offset.

Standard library only.
"""
import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

NOTES_DIR = "notes"
OFFSET_FILE = ".telegram_offset"
PENDING_FILE = ".telegram_pending.json"
TASKS_HEADER = "# Задачи\n\n"
IDEAS_HEADER = "# Идеи\n\n"


def load_token(root: Path) -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token.strip()
    env_file = root / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("TELEGRAM_BOT_TOKEN не найден ни в окружении, ни в .env")


def ssl_context() -> ssl.SSLContext:
    # python.org builds on macOS ship without CA certs; fall back to the system bundle.
    system_bundle = "/etc/ssl/cert.pem"
    if os.path.exists(system_bundle):
        return ssl.create_default_context(cafile=system_bundle)
    return ssl.create_default_context()


def read_offset(notes: Path) -> int | None:
    f = notes / OFFSET_FILE
    if f.exists():
        value = f.read_text().strip()
        return int(value) if value else None
    return None


def author_of(msg: dict) -> str:
    user = msg.get("from") or msg.get("sender_chat") or {}
    if user.get("username"):
        return "@" + user["username"]
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    return "@" + (name or user.get("title") or "unknown")


def fetch(root: Path) -> None:
    token = load_token(root)
    notes = root / NOTES_DIR
    notes.mkdir(exist_ok=True)

    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    offset = read_offset(notes)
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{token}/getUpdates?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30, context=ssl_context()) as resp:
            data = json.load(resp)
    except Exception as e:  # don't leak the token via the URL in tracebacks
        sys.exit(f"Ошибка запроса к Telegram API: {type(e).__name__}: {getattr(e, 'reason', e)}")
    if not data.get("ok"):
        sys.exit(f"Telegram API вернул ошибку: {data.get('description')}")

    updates = data.get("result", [])
    messages, skipped = [], 0
    for upd in updates:
        msg = upd.get("message")
        if not msg or "text" not in msg:
            skipped += 1
            continue
        messages.append({
            "update_id": upd["update_id"],
            "text": msg["text"],
            "date": datetime.fromtimestamp(msg["date"]).strftime("%Y-%m-%d"),
            "author": author_of(msg),
        })

    # Next offset covers skipped non-text updates too, so they don't come back.
    next_offset = max(u["update_id"] for u in updates) + 1 if updates else offset
    pending = {"next_offset": next_offset, "skipped_non_text": skipped, "messages": messages}
    if updates:
        (notes / PENDING_FILE).write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(pending, ensure_ascii=False, indent=2))


def as_list_item(prefix: str, text: str) -> str:
    # Keep multi-line messages inside one list item via indentation.
    lines = text.strip().splitlines() or [""]
    return prefix + lines[0] + "".join("\n  " + l for l in lines[1:])


def append(path: Path, header: str, lines: list[str]) -> None:
    if not lines:
        return
    new_file = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if new_file:
            f.write(header)
        elif path.stat().st_size and not path.read_text(encoding="utf-8").endswith("\n"):
            f.write("\n")
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())


def save(root: Path) -> None:
    notes = root / NOTES_DIR
    pending_file = notes / PENDING_FILE
    if not pending_file.exists():
        sys.exit("Нет notes/.telegram_pending.json — сначала запусти fetch")
    pending = json.loads(pending_file.read_text(encoding="utf-8"))
    by_id = {m["update_id"]: m for m in pending["messages"]}

    # stdin: [{"update_id": 1, "kind": "task"|"idea", "deadline": "завтра"|null}, ...]
    labels = {int(c["update_id"]): c for c in json.load(sys.stdin)}
    missing = set(by_id) - set(labels)
    if missing:
        sys.exit(f"Нет классификации для update_id: {sorted(missing)}")

    tasks, ideas, report = [], [], []
    for uid, m in by_id.items():
        c = labels[uid]
        kind = "task" if c.get("kind") == "task" else "idea"
        meta = f"({m['date']}, {m['author']})"
        if kind == "task":
            deadline = (c.get("deadline") or "").strip()
            suffix = f" — срок: {deadline} {meta}" if deadline else f" {meta}"
            tasks.append(as_list_item("- [ ] ", m["text"]) + suffix)
        else:
            ideas.append(as_list_item("- ", m["text"]) + f" {meta}")
        report.append({"kind": kind, "text": m["text"], "deadline": c.get("deadline")})

    append(notes / "tasks.md", TASKS_HEADER, tasks)
    append(notes / "ideas.md", IDEAS_HEADER, ideas)

    # Advance offset only after both files were written successfully.
    if pending.get("next_offset") is not None:
        (notes / OFFSET_FILE).write_text(str(pending["next_offset"]))
    pending_file.unlink()
    print(json.dumps({"tasks": len(tasks), "ideas": len(ideas), "items": report}, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["fetch", "save"])
    p.add_argument("--root", default=".", help="корень проекта (где лежат .env и notes/)")
    args = p.parse_args()
    root = Path(args.root).resolve()
    fetch(root) if args.command == "fetch" else save(root)


if __name__ == "__main__":
    main()
