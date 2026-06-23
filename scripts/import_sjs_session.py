"""Import SJS-AGENT session (JSONL) → Personal Agent session (SQLite).

Reads SJS-AGENT's JSONL session file and converts it to the Personal Agent's
SQLite session store. Messages are wrapped in Anthropic content-block format.

Usage:
  uv run python scripts/import_sjs_session.py \
    --source "C:/Users/MR/Desktop/SJS-AGENT/prompt_config/.sessions/luna/feishu_*.jsonl" \
    --session-key "wechat:wx_id:wx_id" \
    [--dry-run]

The session-key should match the target WeChat user's session_key
(format: platform:chat_id:user_id).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path


def convert_messages(source_path: Path) -> list[dict]:
    """Read SJS-AGENT JSONL and convert to Personal Agent message format.

    SJS-AGENT format:
      {"role": "user", "content": "text string"}
      {"role": "assistant", "content": "text string"}

    Personal Agent format:
      {"role": "user", "content": [{"type": "text", "text": "..."}]}
      {"role": "assistant", "content": [{"type": "text", "text": "..."}]}
    """
    messages = []

    with open(source_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            role = msg.get("role", "")
            content = msg.get("content", "")

            if role not in ("user", "assistant"):
                continue
            if not content:
                continue

            # Wrap text in Anthropic content block format
            messages.append({
                "role": role,
                "content": [{"type": "text", "text": content}],
            })

    return messages


async def import_session(
    db_path: str,
    session_key: str,
    messages: list[dict],
    dry_run: bool = False,
) -> None:
    """Import converted messages into Personal Agent's SQLite session store."""
    import aiosqlite

    if dry_run:
        print(f"Dry run: would import {len(messages)} messages to session '{session_key}'")
        print(f"Database: {db_path}")
        for i, msg in enumerate(messages[:5]):
            text = msg["content"][0]["text"][:80] if msg.get("content") else ""
            print(f"  [{i}] {msg['role']}: {text}...")
        if len(messages) > 5:
            print(f"  ... and {len(messages) - 5} more")
        return

    db = await aiosqlite.connect(db_path)

    # Ensure schema
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id   TEXT PRIMARY KEY,
            session_key  TEXT NOT NULL,
            platform     TEXT NOT NULL,
            user_id      TEXT NOT NULL,
            user_name    TEXT DEFAULT '',
            chat_id      TEXT DEFAULT '',
            chat_type    TEXT DEFAULT 'dm',
            created_at   REAL NOT NULL,
            last_active_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT NOT NULL REFERENCES sessions(session_id),
            role          TEXT NOT NULL,
            content       TEXT DEFAULT '',
            tool_calls    TEXT DEFAULT NULL,
            tool_name     TEXT DEFAULT NULL,
            tool_call_id  TEXT DEFAULT NULL,
            timestamp     REAL NOT NULL
        );
    """)

    # Parse session_key: platform:chat_id:user_id
    parts = session_key.split(":", 2)
    platform = parts[0] if len(parts) > 0 else "wechat"
    chat_id = parts[1] if len(parts) > 1 else session_key
    user_id = parts[2] if len(parts) > 2 else chat_id

    # Create or get session
    session_id = str(uuid.uuid4())[:16]
    now = time.time()

    await db.execute(
        "INSERT OR REPLACE INTO sessions (session_id, session_key, platform, user_id, "
        "chat_id, created_at, last_active_at, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, session_key, platform, user_id, chat_id, now, now, len(messages)),
    )

    # Insert messages
    for i, msg in enumerate(messages):
        content_json = json.dumps(msg.get("content", []), ensure_ascii=False)
        role = msg.get("role", "user")
        tool_calls = json.dumps(msg.get("tool_calls")) if msg.get("tool_calls") else None

        await db.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content_json, tool_calls, now + i * 0.001),
        )

    await db.commit()
    await db.close()

    print(f"Imported {len(messages)} messages → session '{session_id}' "
          f"(key: {session_key})")


def main():
    parser = argparse.ArgumentParser(description="Import SJS-AGENT session to Personal Agent")
    parser.add_argument("--source", required=True, help="Path to SJS-AGENT .jsonl session file")
    parser.add_argument("--session-key", required=True,
                        help="Target session key (format: platform:chat_id:user_id)")
    parser.add_argument("--db", default="./data/state.db",
                        help="Path to Personal Agent SQLite database")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing")

    args = parser.parse_args()
    source = Path(args.source)
    if not source.exists():
        print(f"Error: source file not found: {args.source}")
        return

    messages = convert_messages(source)
    print(f"Read {len(messages)} messages from {source.name}")

    asyncio.run(import_session(args.db, args.session_key, messages, args.dry_run))


if __name__ == "__main__":
    main()
