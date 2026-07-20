#!/usr/bin/env python3
"""MCP server for macOS iMessage: send/read messages, monitor chats for
replies, and look up/update Contacts.app entries.

Transport: stdio (local, single-user tool driving Messages.app/Contacts.app
via osascript and reading ~/Library/Messages/chat.db directly).

Requires (System Settings > Privacy & Security):
  - Full Disk Access, for the process running this server (to read chat.db)
  - Automation, for the same process to control Messages.app and Contacts.app
"""
from __future__ import annotations

import asyncio
import json
from enum import Enum
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from . import applescript, contacts, db
from .dates import iso_to_apple_ns

mcp = FastMCP("imessage_mcp")


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


def _json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _pretty_label(label: str) -> str:
    """Render Contacts.app's raw '_$!<Home>!$_'-style label constants as 'Home'."""
    if label.startswith("_$!<") and label.endswith(">!$_"):
        return label[4:-4]
    return label


def _error_result(message: str) -> str:
    return _json({"error": message})


# ---------------------------------------------------------------------------
# Messages: send
# ---------------------------------------------------------------------------

class SendMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    recipient: str = Field(
        ...,
        description="Phone number or email address to send to (any format, "
                     "e.g. '+1 555-123-4567' or 'name@example.com'). For an "
                     "existing group chat, pass its chat identifier/guid "
                     "instead (contains ';').",
        min_length=3,
        max_length=200,
    )
    text: str = Field(..., description="Message body to send.", min_length=1, max_length=8000)


@mcp.tool(
    name="imessage_send_message",
    annotations={
        "title": "Send an iMessage/SMS",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def imessage_send_message(params: SendMessageInput) -> str:
    """Send a message to a phone number, email, or existing group chat.

    Tries iMessage first (via an existing chat if one exists, or a new
    iMessage conversation otherwise); if that cannot be verified as
    delivered within 15 seconds, retries once over SMS/RCS. Actually sends
    a real message to a real person — this is not a dry run.

    Args:
        params (SendMessageInput):
            - recipient (str): Phone number, email, or group chat identifier.
            - text (str): Message body.

    Returns:
        str: JSON with schema:
        Success: {"sent": true, "service": "iMessage"|"SMS"|"RCS", "date": <ISO8601>, "attempts": [...]}
        Failure: {"error": "<message>"}

    Error Handling:
        - Returns an error if the message cannot be verified as sent after
          both the iMessage and SMS attempts (e.g. missing Automation
          permission, or no valid route to the recipient).
    """
    try:
        result = await applescript.send_message(params.recipient, params.text)
        return _json(result)
    except (applescript.SendError, db.ChatDBError) as e:
        return _error_result(str(e))


# ---------------------------------------------------------------------------
# Messages: list chats
# ---------------------------------------------------------------------------

class ListChatsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: Optional[str] = Field(
        default=None,
        description="Filter chats whose identifier (phone/email) or group "
                     "display name contains this substring. Omit to list "
                     "the most recently active chats.",
        max_length=200,
    )
    limit: int = Field(default=20, ge=1, le=100, description="Maximum chats to return.")
    offset: int = Field(default=0, ge=0, description="Number of chats to skip, for pagination.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format.")


@mcp.tool(
    name="imessage_list_chats",
    annotations={
        "title": "List Message Chats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def imessage_list_chats(params: ListChatsInput) -> str:
    """List recent Messages conversations, most recently active first.

    Use this to discover the identifier (phone number, email, or group
    chat guid) for a conversation when you don't already have it. Note
    this does not know contact names for 1:1 chats (only group display
    names) — cross-reference with imessage_search_contacts to resolve a
    person's name to their phone number/email first.

    Args:
        params (ListChatsInput): Validated input containing:
            - query (Optional[str]): Substring filter on identifier/display name.
            - limit (int): Max results (1-100, default 20).
            - offset (int): Pagination offset (default 0).
            - response_format (ResponseFormat): "markdown" or "json".

    Returns:
        str: Markdown listing or JSON with schema:
        {
            "total": int, "count": int, "offset": int, "has_more": bool, "next_offset": int|null,
            "chats": [{"identifier": str, "display_name": str|null, "guid": str,
                       "last_message_date": ISO8601, "last_message_preview": str|null,
                       "last_message_from_me": bool|null, "message_count": int}]
        }

    Error Handling:
        - Returns {"error": ...} if chat.db cannot be read (see Full Disk
          Access requirement in the server description).
    """
    try:
        data = await asyncio.to_thread(db.list_chats, params.query, params.limit, params.offset)
    except db.ChatDBError as e:
        return _error_result(str(e))

    if params.response_format == ResponseFormat.JSON:
        return _json(data)

    if not data["chats"]:
        return "No chats found."
    lines = [f"# Chats ({data['total']} total, showing {data['count']})", ""]
    for c in data["chats"]:
        title = c["display_name"] or c["identifier"]
        lines.append(f"## {title} ({c['identifier']})")
        lines.append(f"- Last message: {c['last_message_date']}")
        if c["last_message_preview"]:
            who = "You" if c["last_message_from_me"] else title
            lines.append(f"- Preview: {who}: {c['last_message_preview']}")
        lines.append(f"- Messages: {c['message_count']}")
        lines.append("")
    if data["has_more"]:
        lines.append(f"...has more (next_offset={data['next_offset']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Messages: list messages in a chat
# ---------------------------------------------------------------------------

class ListMessagesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    identifier: str = Field(
        ...,
        description="Chat identifier: a phone number, email, or group chat "
                     "identifier, as returned by imessage_list_chats.",
        min_length=1,
        max_length=200,
    )
    limit: int = Field(default=50, ge=1, le=200, description="Maximum messages to return.")
    offset: int = Field(default=0, ge=0, description="Number of most-recent messages to skip, for pagination.")
    before: Optional[str] = Field(
        default=None,
        description="Only return messages strictly before this ISO8601 timestamp.",
    )
    include_reactions: bool = Field(default=False, description="Include tapback reactions (thumbs up, heart, etc).")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format.")


@mcp.tool(
    name="imessage_list_messages",
    annotations={
        "title": "List Messages In A Chat",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def imessage_list_messages(params: ListMessagesInput) -> str:
    """Fetch message history for one chat (1:1 or group), newest page first
    internally but returned in chronological order for readability.

    Args:
        params (ListMessagesInput): Validated input containing:
            - identifier (str): Phone number, email, or chat identifier.
            - limit (int): Max messages (1-200, default 50).
            - offset (int): Skip this many of the most recent messages first (default 0).
            - before (Optional[str]): ISO8601 cutoff; only older messages are returned.
            - include_reactions (bool): Include tapbacks (default False).
            - response_format (ResponseFormat): "markdown" or "json".

    Returns:
        str: Markdown transcript or JSON with schema:
        {
            "total": int, "count": int, "offset": int, "has_more": bool, "next_offset": int|null,
            "messages": [{"id": int, "date": ISO8601, "from_me": bool, "text": str|null,
                          "service": str|null, "delivery_error": bool, "sent": bool, "is_reaction": bool}]
        }

    Error Handling:
        - Returns {"error": ...} if no chat matches `identifier` (suggests
          using imessage_list_chats or imessage_search_contacts first), or
          if chat.db cannot be read.
    """
    try:
        before_ns = iso_to_apple_ns(params.before) if params.before else None
        data = await asyncio.to_thread(
            db.list_messages, params.identifier, params.limit, params.offset, before_ns, params.include_reactions
        )
    except db.ChatDBError as e:
        return _error_result(str(e))
    except ValueError:
        return _error_result(f"Invalid 'before' timestamp: '{params.before}'. Use ISO8601 format.")

    if params.response_format == ResponseFormat.JSON:
        return _json(data)

    if not data["messages"]:
        return f"No messages found for '{params.identifier}'."
    lines = [f"# Messages with {params.identifier} ({data['total']} total, showing {data['count']})", ""]
    for m in data["messages"]:
        who = "You" if m["from_me"] else params.identifier
        marker = " [reaction]" if m["is_reaction"] else ""
        lines.append(f"**{m['date']}** {who}{marker}: {m['text'] or '<no text>'}")
    if data["has_more"]:
        lines.append("")
        lines.append(f"...has more (next_offset={data['next_offset']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Messages: wait for a reply (replaces manual polling scripts)
# ---------------------------------------------------------------------------

class WaitForReplyInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    identifier: Optional[str] = Field(
        default=None,
        description="Only watch this chat identifier (phone/email/group id). "
                     "Omit to watch every chat for any new incoming message.",
        max_length=200,
    )
    timeout_seconds: int = Field(default=300, ge=10, le=1800, description="Max seconds to wait before giving up.")
    poll_interval_seconds: int = Field(default=5, ge=1, le=60, description="Seconds between checks.")
    since: Optional[str] = Field(
        default=None,
        description="ISO8601 timestamp; only messages strictly after this "
                     "count as new. Defaults to the moment this tool is called.",
    )
    include_reactions: bool = Field(default=False, description="Also wake on tapback reactions.")


@mcp.tool(
    name="imessage_wait_for_reply",
    annotations={
        "title": "Wait For A Message Reply",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def imessage_wait_for_reply(params: WaitForReplyInput) -> str:
    """Block until a new incoming message arrives (from one contact, or from
    anyone), or until the timeout elapses.

    This replaces manually polling chat.db in a loop: call this once and it
    will hold the connection open (up to timeout_seconds, capped at 1800s /
    30 minutes) and return as soon as something new shows up.

    Args:
        params (WaitForReplyInput): Validated input containing:
            - identifier (Optional[str]): Restrict to one chat; omit for any chat.
            - timeout_seconds (int): Max wait, 10-1800 (default 300).
            - poll_interval_seconds (int): Check interval, 1-60 (default 5).
            - since (Optional[str]): ISO8601 baseline; defaults to call time.
            - include_reactions (bool): Also wake on tapbacks (default False).

    Returns:
        str: JSON with schema:
        {"replied": bool, "waited_seconds": float, "messages": [<same shape as imessage_list_messages items, plus "chat_identifier">]}

        `replied` is false only if the timeout elapsed with nothing new.

    Examples:
        - Use when: "let me know when Sarah responds" -> identifier="+15551234567"
        - Use when: "ping me on the next text from anyone" -> identifier omitted
        - Don't use when: you just want history (use imessage_list_messages instead)
    """
    try:
        since_ns = iso_to_apple_ns(params.since) if params.since else None
    except ValueError:
        return _error_result(f"Invalid 'since' timestamp: '{params.since}'. Use ISO8601 format.")

    result = await db.wait_for_reply(
        params.identifier, params.timeout_seconds, params.poll_interval_seconds, since_ns, params.include_reactions
    )
    return _json(result)


# ---------------------------------------------------------------------------
# Contacts: search / get
# ---------------------------------------------------------------------------

class SearchContactsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Substring to match against contact names.", min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=100, description="Maximum contacts to return.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format.")


@mcp.tool(
    name="imessage_search_contacts",
    annotations={
        "title": "Search Contacts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def imessage_search_contacts(params: SearchContactsInput) -> str:
    """Search Contacts.app by name substring.

    Note Contacts.app frequently has duplicate person records for the same
    real contact; this returns every matching card, so calling code that
    needs to keep a contact's info consistent may need to update more than
    one id (see imessage_update_contact_address / imessage_update_contact_phone).

    Args:
        params (SearchContactsInput): Validated input containing:
            - query (str): Name substring to search for.
            - limit (int): Max results (1-100, default 20).
            - response_format (ResponseFormat): "markdown" or "json".

    Returns:
        str: Markdown listing or JSON list of:
        {"id": str, "name": str, "phones": [{"label": str, "value": str}],
         "emails": [{"label": str, "value": str}],
         "addresses": [{"label": str, "street": str, "city": str, "state": str, "zip": str, "country": str}]}

        Or "No contacts found matching '<query>'" if empty.

    Error Handling:
        - Returns {"error": ...} if Contacts.app automation is not permitted.
    """
    try:
        records = await contacts.search_contacts(params.query, params.limit)
    except contacts.ContactsError as e:
        return _error_result(str(e))

    if not records:
        return f"No contacts found matching '{params.query}'"

    if params.response_format == ResponseFormat.JSON:
        return _json(records)

    lines = [f"# Contacts matching '{params.query}' ({len(records)})", ""]
    for r in records:
        lines.append(f"## {r['name']} ({r['id']})")
        for p in r["phones"]:
            lines.append(f"- Phone ({_pretty_label(p['label'])}): {p['value']}")
        for e in r["emails"]:
            lines.append(f"- Email ({_pretty_label(e['label'])}): {e['value']}")
        for a in r["addresses"]:
            lines.append(f"- Address ({_pretty_label(a['label'])}): {a['street']}, {a['city']}, {a['state']} {a['zip']}, {a['country']}")
        lines.append("")
    return "\n".join(lines)


class GetContactInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    contact_id: str = Field(..., description="Contact id, as returned by imessage_search_contacts.", min_length=1)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format.")


@mcp.tool(
    name="imessage_get_contact",
    annotations={
        "title": "Get Contact Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def imessage_get_contact(params: GetContactInput) -> str:
    """Fetch full details (phones, emails, addresses) for one contact by id.

    Args:
        params (GetContactInput): Validated input containing:
            - contact_id (str): Contact id from imessage_search_contacts.
            - response_format (ResponseFormat): "markdown" or "json".

    Returns:
        str: Same record shape as one item of imessage_search_contacts, or
        "Contact not found: '<contact_id>'" if it doesn't exist.

    Error Handling:
        - Returns {"error": ...} if Contacts.app automation is not permitted.
    """
    try:
        record = await contacts.get_contact(params.contact_id)
    except contacts.ContactsError as e:
        return _error_result(str(e))

    if not record:
        return f"Contact not found: '{params.contact_id}'"

    if params.response_format == ResponseFormat.JSON:
        return _json(record)

    lines = [f"# {record['name']} ({record['id']})", ""]
    for p in record["phones"]:
        lines.append(f"- Phone ({_pretty_label(p['label'])}): {p['value']}")
    for e in record["emails"]:
        lines.append(f"- Email ({_pretty_label(e['label'])}): {e['value']}")
    for a in record["addresses"]:
        lines.append(f"- Address ({_pretty_label(a['label'])}): {a['street']}, {a['city']}, {a['state']} {a['zip']}, {a['country']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Contacts: create / update
# ---------------------------------------------------------------------------

class CreateContactInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    first_name: str = Field(..., description="First name.", min_length=1, max_length=100)
    last_name: Optional[str] = Field(default=None, description="Last name.", max_length=100)
    phone: Optional[str] = Field(default=None, description="Phone number to add, if any.", max_length=50)
    phone_label: str = Field(default="mobile", description="Label for the phone: home, work, mobile, main, or other.")
    email: Optional[str] = Field(default=None, description="Email address to add, if any.", max_length=200)
    email_label: str = Field(default="home", description="Label for the email: home, work, mobile, main, or other.")


@mcp.tool(
    name="imessage_create_contact",
    annotations={
        "title": "Create Contact",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def imessage_create_contact(params: CreateContactInput) -> str:
    """Create a new Contacts.app card. Does not check for existing contacts
    with the same name first — call imessage_search_contacts beforehand to
    avoid creating a duplicate.

    Args:
        params (CreateContactInput): Validated input containing:
            - first_name (str), last_name (Optional[str])
            - phone (Optional[str]), phone_label (str, default "mobile")
            - email (Optional[str]), email_label (str, default "home")

    Returns:
        str: JSON {"created": true, "id": "<new contact id>"} or {"error": ...}
    """
    try:
        new_id = await contacts.create_contact(
            params.first_name, params.last_name,
            params.phone, params.phone_label,
            params.email, params.email_label,
        )
        return _json({"created": True, "id": new_id})
    except contacts.ContactsError as e:
        return _error_result(str(e))


class UpdateContactAddressInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    contact_id: str = Field(..., description="Contact id, as returned by imessage_search_contacts.", min_length=1)
    street: str = Field(..., description="Street address line.", min_length=1, max_length=200)
    city: str = Field(..., description="City.", min_length=1, max_length=100)
    state: str = Field(default="", description="State/province.", max_length=100)
    zip_code: str = Field(default="", description="Postal/ZIP code.", max_length=20)
    country: str = Field(default="", description="Country.", max_length=100)
    label: str = Field(default="home", description="Address label: home, work, or other. Updates the existing address with this label if one exists, otherwise adds a new one.")


@mcp.tool(
    name="imessage_update_contact_address",
    annotations={
        "title": "Update Contact Address",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def imessage_update_contact_address(params: UpdateContactAddressInput) -> str:
    """Add or replace one address (by label) on an existing contact card.

    If the contact already has an address with the given label, its fields
    are overwritten in place; otherwise a new address is added. Uses the
    native Contacts.app label constants under the hood so the label
    displays correctly in the Contacts UI (rather than as "Other").

    Args:
        params (UpdateContactAddressInput): Validated input containing:
            - contact_id (str), street (str), city (str)
            - state, zip_code, country (str, all optional)
            - label (str): "home", "work", or "other" (default "home")

    Returns:
        str: JSON {"updated": true} or {"error": ...}

    Error Handling:
        - Returns {"error": ...} if contact_id does not exist, or if
          Contacts.app automation is not permitted.
    """
    try:
        await contacts.update_contact_address(
            params.contact_id, params.street, params.city, params.state,
            params.zip_code, params.country, params.label,
        )
        return _json({"updated": True})
    except contacts.ContactsError as e:
        return _error_result(str(e))


class UpdateContactPhoneInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    contact_id: str = Field(..., description="Contact id, as returned by imessage_search_contacts.", min_length=1)
    phone: str = Field(..., description="Phone number.", min_length=3, max_length=50)
    label: str = Field(default="mobile", description="Phone label: home, work, mobile, main, or other. Updates the existing phone with this label if one exists, otherwise adds a new one.")


@mcp.tool(
    name="imessage_update_contact_phone",
    annotations={
        "title": "Update Contact Phone",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def imessage_update_contact_phone(params: UpdateContactPhoneInput) -> str:
    """Add or replace one phone number (by label) on an existing contact card.

    If the contact already has a phone with the given label, its value is
    overwritten; otherwise a new phone entry is added.

    Args:
        params (UpdateContactPhoneInput): Validated input containing:
            - contact_id (str), phone (str)
            - label (str): "home", "work", "mobile", "main", or "other" (default "mobile")

    Returns:
        str: JSON {"updated": true} or {"error": ...}

    Error Handling:
        - Returns {"error": ...} if contact_id does not exist, or if
          Contacts.app automation is not permitted.
    """
    try:
        await contacts.update_contact_phone(params.contact_id, params.phone, params.label)
        return _json({"updated": True})
    except contacts.ContactsError as e:
        return _error_result(str(e))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
