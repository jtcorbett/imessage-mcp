"""Sending iMessage/SMS via Messages.app, driven through osascript.

Requires Automation permission for the process running this server to
control Messages.app (System Settings > Privacy & Security > Automation).
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

from . import db
from .dates import apple_ns_to_iso

_CHAT_STMT = 'tell application "Messages" to send (item 1 of argv) to chat id (item 2 of argv)'
_IMSG_STMT = (
    'tell application "Messages" to send (item 1 of argv) to '
    'participant (item 2 of argv) of (1st account whose service type = iMessage)'
)
_SMS_STMT = (
    'tell application "Messages" to send (item 1 of argv) to '
    'participant (item 2 of argv) of (1st account whose service type = SMS)'
)

_VERIFY_TIMEOUT = 15
_VERIFY_POLL = 1


class SendError(Exception):
    pass


def _e164_like(recipient: str) -> str:
    return re.sub(r"[^\d+]", "", recipient)


async def _run_osascript(statement: str, text: str, target: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", "on run argv", "-e", statement, "-e", "end run", text, target,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    return proc.returncode, stderr.decode().strip()


async def _verify_sent(identifier: str, baseline_date: int) -> Optional[tuple[int, str, int, bool]]:
    start = time.monotonic()
    while time.monotonic() - start < _VERIFY_TIMEOUT:
        status = await asyncio.to_thread(db.last_outgoing_status, identifier)
        if status and status[0] > baseline_date:
            return status
        await asyncio.sleep(_VERIFY_POLL)
    return None


async def send_message(recipient: str, text: str) -> dict:
    """Send an iMessage, falling back to SMS/RCS if iMessage delivery fails.

    `recipient` should be a phone number (any format) or email address.
    Group chats are not supported by this fallback chain; pass an existing
    chat's identifier only for 1:1 conversations.
    """
    is_group_guid = ";" in recipient
    identifier = recipient if is_group_guid else _e164_like(recipient)

    baseline = db.last_outgoing_status(identifier)
    baseline_date = baseline[0] if baseline else 0

    guid = None if is_group_guid else db.chat_guid_for_identifier(identifier)
    target = guid or identifier
    stmt = _CHAT_STMT if guid else _IMSG_STMT

    rc, stderr = await _run_osascript(stmt, text, target)
    attempts = [{"method": "chat" if guid else "iMessage participant", "rc": rc, "stderr": stderr}]

    status = await _verify_sent(identifier, baseline_date)

    if not status and not is_group_guid:
        rc2, stderr2 = await _run_osascript(_SMS_STMT, text, identifier)
        attempts.append({"method": "SMS participant (retry)", "rc": rc2, "stderr": stderr2})
        status = await _verify_sent(identifier, baseline_date)

    if not status:
        raise SendError(
            f"Message could not be verified as sent to '{recipient}' after "
            f"{len(attempts)} attempt(s). This usually means Messages.app "
            "does not have a valid route to this recipient, or Automation "
            "permission has not been granted. Attempts: "
            f"{attempts}"
        )

    date_apple_ns, service, _error, _is_sent = status
    return {
        "sent": True,
        "service": service,
        "date": apple_ns_to_iso(date_apple_ns),
        "attempts": attempts,
    }
