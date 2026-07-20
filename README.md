# imessage_mcp

Local MCP server for macOS Messages + Contacts: send messages, read chat
history, wait for a reply without polling manually, and look up/update
Contacts.app entries.

Runs over stdio, driven directly by whatever agent/client launches it
(e.g. Claude Code, Claude Desktop). Only tested on and intended for macOS.

## Setup

```bash
cd imessage-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### macOS permissions

The **process that actually runs this server** (not your terminal, unless
that's what launches it) needs, under System Settings > Privacy & Security:

- **Full Disk Access** — to read `~/Library/Messages/chat.db` directly.
- **Automation** — to control Messages.app and Contacts.app via `osascript`.

macOS prompts for these the first time each permission is needed; if a
prompt doesn't appear, add the process manually in System Settings. If
you're running this via Claude Code/Desktop, that's typically the app
itself (or the terminal it was launched from) that needs the grant, not
the Python interpreter.

### Register with Claude Code

```bash
claude mcp add imessage-mcp -- bash -c "cd /Users/john/mcp/imessage-mcp && .venv/bin/python -m imessage_mcp.server"
```

The `cd` wrapper is required: this must run as `python -m imessage_mcp.server`
(a package, for the relative imports between modules) rather than by direct
script path, and `-m` only finds the package if the process's working
directory is the project root -- which isn't guaranteed to be true when
Claude Code launches it from wherever *you* happen to be working.

Use `-s user` instead of the default `local` scope if you want it available
across all projects, not just this directory.

## Tools

| Tool | What it does |
|---|---|
| `imessage_send_message` | Send to a phone/email/group chat; tries iMessage, falls back to SMS/RCS, verifies delivery. |
| `imessage_list_chats` | List recent conversations with previews. |
| `imessage_list_messages` | Paginated message history for one chat. |
| `imessage_wait_for_reply` | Block (up to 30 min) until a new incoming message arrives — replaces hand-rolled polling loops. |
| `imessage_search_contacts` | Search Contacts.app by name. |
| `imessage_get_contact` | Full details (phones/emails/addresses) for one contact id. |
| `imessage_create_contact` | Create a new Contacts.app card. |
| `imessage_update_contact_address` | Add/replace an address by label (home/work/other). |
| `imessage_update_contact_phone` | Add/replace a phone by label (home/work/mobile/main/other). |

## Known limitations

- **Contacts.app duplicate cards**: it's common for the same real person to
  have 2+ separate `person` records. `imessage_search_contacts` returns all
  of them; keeping a contact's info consistent may mean calling the update
  tools once per duplicate id.
- **Contact ids can go stale**: with iCloud Contacts sync on, a person's
  `id` can change shortly after it's created or edited (sync reassigns/
  merges the record). If an update call errors saying the contact wasn't
  found, re-run `imessage_search_contacts` to get the current id and retry.
- **No delete/merge tools**: intentionally out of scope, since this is
  meant to be low-risk to run against real data.
- **`imessage_send_message` group chat support**: only works for an
  *existing* group chat identifier; it won't create a new group.
- Requires macOS Messages/Contacts apps to be installed and signed in; no
  support for other platforms.

## Notes on implementation

- Message dates in `chat.db` are nanoseconds since 2001-01-01 (Apple's Core
  Data epoch); `dates.py` handles the conversion to/from ISO8601.
- Structured Contacts.app output is parsed out of AppleScript using ASCII
  separator control characters (record/group/unit/field separators)
  generated at runtime via `(ASCII character N)`, rather than hand-built
  JSON in AppleScript. These are generated at runtime rather than embedded
  as literal bytes in the script source, because Python's
  `str.splitlines()` (used to break a script into `-e` arguments) treats
  `\x1c`/`\x1d`/`\x1e` as line boundaries and would silently corrupt them.
- All dynamic values (search queries, addresses, phone numbers) are passed
  as `argv` items to `osascript`, never interpolated into the script
  source, to avoid AppleScript injection.
