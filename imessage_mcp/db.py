"""Read-only access to the macOS Messages database (~/Library/Messages/chat.db).

Requires the process running this server to have Full Disk Access
(System Settings > Privacy & Security > Full Disk Access).
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

from .dates import apple_ns_to_iso, now_apple_ns

DB_PATH = "file:" + os.path.expanduser("~/Library/Messages/chat.db") + "?mode=ro"


class ChatDBError(Exception):
    """Raised when chat.db cannot be read (usually a permissions problem)."""


def _connect() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(DB_PATH, uri=True, timeout=5)
        # Fail fast if we truly can't read anything from the db.
        conn.execute("SELECT 1 FROM message LIMIT 1")
        return conn
    except sqlite3.OperationalError as e:
        raise ChatDBError(
            "Cannot read the Messages database. Grant Full Disk Access to the "
            "process running this MCP server (System Settings > Privacy & "
            "Security > Full Disk Access), then restart the server."
        ) from e


def decode_attributed_body(blob: Optional[bytes]) -> Optional[str]:
    """Best-effort extraction of message text from a legacy attributedBody blob."""
    if not blob:
        return None
    idx = blob.find(b"NSString")
    if idx == -1:
        return None
    plus = blob.find(b"+", idx)
    if plus == -1:
        return None
    i = plus + 1
    length = blob[i]
    i += 1
    if length == 0x81:
        length = int.from_bytes(blob[i:i + 2], "little")
        i += 2
    try:
        return blob[i:i + length].decode("utf-8", errors="replace")
    except Exception:
        return None


@dataclass
class Message:
    rowid: int
    date_iso: str
    date_apple_ns: int
    is_from_me: bool
    text: Optional[str]
    service: Optional[str]
    error: int
    is_sent: bool
    is_reaction: bool

    def as_dict(self) -> dict:
        return {
            "id": self.rowid,
            "date": self.date_iso,
            "from_me": self.is_from_me,
            "text": self.text,
            "service": self.service,
            "delivery_error": bool(self.error),
            "sent": self.is_sent,
            "is_reaction": self.is_reaction,
        }


def _row_to_message(row) -> Message:
    rowid, date, is_from_me, text, blob, service, error, is_sent, amt = row
    body = text or decode_attributed_body(blob)
    return Message(
        rowid=rowid,
        date_iso=apple_ns_to_iso(date),
        date_apple_ns=date,
        is_from_me=bool(is_from_me),
        text=body,
        service=service,
        error=error or 0,
        is_sent=bool(is_sent),
        is_reaction=bool(amt),
    )


def chat_guid_for_identifier(identifier: str) -> Optional[str]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT guid FROM chat WHERE chat_identifier = ?", (identifier,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def last_outgoing_status(identifier: str) -> Optional[tuple[int, str, int, bool]]:
    """Return (date_apple_ns, service, error, is_sent) for the most recent
    outgoing message to this chat identifier, or None if there is none yet."""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT m.date, m.service, m.error, m.is_sent
               FROM message m
               JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
               JOIN chat c ON c.ROWID = cmj.chat_id
               WHERE c.chat_identifier = ? AND m.is_from_me = 1
               ORDER BY m.date DESC LIMIT 1""",
            (identifier,),
        ).fetchone()
        if not row:
            return None
        date, service, error, is_sent = row
        return date, service, error or 0, bool(is_sent)
    finally:
        conn.close()


def list_chats(query: Optional[str], limit: int, offset: int) -> dict:
    conn = _connect()
    try:
        where = ""
        params: list = []
        if query:
            where = "WHERE c.chat_identifier LIKE ? OR c.display_name LIKE ?"
            like = f"%{query}%"
            params.extend([like, like])

        total = conn.execute(
            f"SELECT COUNT(DISTINCT c.ROWID) FROM chat c {where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"""SELECT c.chat_identifier, c.display_name, c.guid,
                       MAX(m.date) as last_date, COUNT(m.ROWID) as msg_count
                FROM chat c
                JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
                JOIN message m ON m.ROWID = cmj.message_id
                {where}
                GROUP BY c.ROWID
                ORDER BY last_date DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

        chats = []
        for identifier, display_name, guid, last_date, msg_count in rows:
            preview_row = conn.execute(
                """SELECT m.text, m.attributedBody, m.is_from_me
                   FROM message m
                   JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                   JOIN chat c ON c.ROWID = cmj.chat_id
                   WHERE c.chat_identifier = ?
                     AND COALESCE(m.associated_message_type, 0) = 0
                   ORDER BY m.date DESC LIMIT 1""",
                (identifier,),
            ).fetchone()
            preview = None
            from_me = None
            if preview_row:
                text, blob, is_from_me = preview_row
                preview = text or decode_attributed_body(blob)
                from_me = bool(is_from_me)
            chats.append({
                "identifier": identifier,
                "display_name": display_name,
                "guid": guid,
                "last_message_date": apple_ns_to_iso(last_date),
                "last_message_preview": preview,
                "last_message_from_me": from_me,
                "message_count": msg_count,
            })

        return {
            "total": total,
            "count": len(chats),
            "offset": offset,
            "chats": chats,
            "has_more": total > offset + len(chats),
            "next_offset": offset + len(chats) if total > offset + len(chats) else None,
        }
    finally:
        conn.close()


def list_messages(
    identifier: str,
    limit: int,
    offset: int,
    before_apple_ns: Optional[int],
    include_reactions: bool,
) -> dict:
    conn = _connect()
    try:
        conditions = ["c.chat_identifier = ?"]
        params: list = [identifier]
        if not include_reactions:
            conditions.append("COALESCE(m.associated_message_type, 0) = 0")
        if before_apple_ns is not None:
            conditions.append("m.date < ?")
            params.append(before_apple_ns)
        where = " AND ".join(conditions)

        total = conn.execute(
            f"""SELECT COUNT(*) FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE {where}""",
            params,
        ).fetchone()[0]

        if total == 0:
            exists = conn.execute(
                "SELECT 1 FROM chat WHERE chat_identifier = ?", (identifier,)
            ).fetchone()
            if not exists:
                raise ChatDBError(
                    f"No chat found for identifier '{identifier}'. Use "
                    "imessage_list_chats to find the correct identifier, or "
                    "imessage_search_contacts to resolve a name to a phone number."
                )

        rows = conn.execute(
            f"""SELECT m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody,
                       m.service, m.error, m.is_sent,
                       COALESCE(m.associated_message_type, 0)
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE {where}
                ORDER BY m.date DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

        messages = [_row_to_message(r).as_dict() for r in rows]
        messages.reverse()  # chronological order for reading

        return {
            "total": total,
            "count": len(messages),
            "offset": offset,
            "messages": messages,
            "has_more": total > offset + len(messages),
            "next_offset": offset + len(messages) if total > offset + len(messages) else None,
        }
    finally:
        conn.close()


def _poll_new_incoming(identifier: Optional[str], since_apple_ns: int, include_reactions: bool) -> list[Message]:
    conn = _connect()
    try:
        conditions = ["m.is_from_me = 0", "m.date > ?"]
        params: list = [since_apple_ns]
        if identifier:
            conditions.append("c.chat_identifier = ?")
            params.append(identifier)
        if not include_reactions:
            conditions.append("COALESCE(m.associated_message_type, 0) = 0")
        where = " AND ".join(conditions)

        rows = conn.execute(
            f"""SELECT m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody,
                       m.service, m.error, m.is_sent,
                       COALESCE(m.associated_message_type, 0), c.chat_identifier
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE {where}
                ORDER BY m.date ASC""",
            params,
        ).fetchall()
        results = []
        for r in rows:
            msg = _row_to_message(r[:9])
            d = msg.as_dict()
            d["chat_identifier"] = r[9]
            results.append(d)
        return results
    finally:
        conn.close()


async def wait_for_reply(
    identifier: Optional[str],
    timeout_seconds: int,
    poll_interval_seconds: int,
    since_apple_ns: Optional[int],
    include_reactions: bool,
) -> dict:
    baseline = since_apple_ns if since_apple_ns is not None else now_apple_ns()
    start = time.monotonic()
    while True:
        hits = await asyncio.to_thread(_poll_new_incoming, identifier, baseline, include_reactions)
        if hits:
            return {"replied": True, "waited_seconds": round(time.monotonic() - start, 1), "messages": hits}
        if time.monotonic() - start >= timeout_seconds:
            return {"replied": False, "waited_seconds": round(time.monotonic() - start, 1), "messages": []}
        await asyncio.sleep(poll_interval_seconds)
