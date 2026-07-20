# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local, stdio-transport MCP server exposing macOS Messages + Contacts.app to an
agent/client (Claude Code, Claude Desktop). macOS-only. See `README.md` for the
full tool table, macOS permission setup, and known limitations.

## Commands

```bash
# Setup
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Run the server (must be launched as a package via -m, from the repo root,
# so the relative imports between modules resolve)
.venv/bin/python -m imessage_mcp.server

# Register with Claude Code (the `cd` wrapper is required — see README)
claude mcp add imessage-mcp -- bash -c "cd /Users/john/mcp/imessage-mcp && .venv/bin/python -m imessage_mcp.server"
```

There is no test suite, linter, or build step configured. Manual verification
means driving the tools through a connected MCP client against real
Messages/Contacts data.

## Runtime requirements

The **process that launches the server** (usually the client app or its
terminal, not the Python interpreter) needs two macOS grants under System
Settings > Privacy & Security:
- **Full Disk Access** — to read `~/Library/Messages/chat.db`.
- **Automation** — to control Messages.app and Contacts.app via `osascript`.

## Architecture

Two distinct data paths — do not conflate them:

- **Reads** go through `db.py`: direct **read-only** SQLite against
  `chat.db` (`?mode=ro`). Message history, chat lists, reply-polling.
- **Writes / all Contacts access** go through `osascript` (AppleScript):
  `applescript.py` (send messages) and `contacts.py` (search/create/update
  contacts). There is no write path into `chat.db`.

Layers:
- `server.py` — the only MCP surface. Each tool is a thin FastMCP wrapper: a
  pydantic input model (`extra="forbid"`, whitespace-stripped), then a call
  into `db`/`applescript`/`contacts`, then markdown- or JSON-formatted output.
  Tools return a JSON `{"error": ...}` string on the module-specific exceptions
  (`db.ChatDBError`, `applescript.SendError`, `contacts.ContactsError`) rather
  than raising.
- `db.py` — chat.db queries. Blocking `sqlite3` calls are always dispatched via
  `asyncio.to_thread` from the async layer. `wait_for_reply` is a poll loop
  over `_poll_new_incoming` (this is the intended replacement for hand-rolled
  polling on the client side).
- `applescript.py` — `send_message` is send-then-verify: fire the AppleScript,
  then confirm via `db.last_outgoing_status` that a newer outgoing row appeared
  within `_VERIFY_TIMEOUT` (15s). iMessage is tried first (via an existing chat
  guid if one exists, else a new iMessage participant); if unverified and not a
  group, it retries once over SMS. Success is defined by the db check, not by
  osascript's return code.
- `contacts.py` — see conventions below.
- `dates.py` — all epoch conversion. chat.db stores **nanoseconds since
  2001-01-01Z** (Apple Core Data epoch); every timestamp crossing the boundary
  goes through here. Tool inputs/outputs are ISO8601; internal db values are
  Apple-ns.

## Conventions specific to this codebase

- **Never interpolate dynamic values into AppleScript source.** Every dynamic
  value (query, phone, address, message text, recipient) is passed as an
  `argv` item to `osascript` and read via `item N of argv`, to prevent
  AppleScript injection. Preserve this when editing any `osascript` call.
- **Contacts structured output** is encoded with ASCII separator control chars
  (RS/GS/US/FS = chr 30/29/31/28), parsed in `_parse_contact_records`, rather
  than building JSON inside AppleScript. Those separators are generated at
  AppleScript runtime via `(ASCII character N)` and the script is split on
  `"\n"` **explicitly, not `str.splitlines()`** — `splitlines()` also breaks on
  \x1c/\x1d/\x1e and would corrupt the script. Keep both details if you touch
  `contacts.py`.
- **Contact labels**: user-facing labels ("home"/"work"/"mobile"/…) map to
  native Contacts constants (`_$!<Home>!$_`) via `_native_label` on the way in,
  and are prettified back via `_pretty_label` (server.py) on the way out.
- **Message text** can live in either `message.text` or a legacy
  `attributedBody` blob; `decode_attributed_body` extracts from the blob and is
  applied wherever message text is read.
- **Reactions/tapbacks** are filtered by `associated_message_type` and are
  excluded by default (`include_reactions=False`).
- Contact ids can go stale under iCloud sync; `contacts.py` detects the
  AppleScript "Invalid index" / `-1719` error and returns a `ContactsError`
  telling the caller to re-resolve the id via search. Preserve that mapping.
