"""Read/write access to Contacts.app, driven through osascript.

Requires Automation permission for the process running this server to
control Contacts.app (System Settings > Privacy & Security > Automation).

All dynamic values are passed as `argv` items to the AppleScript (never
interpolated into the script source) to avoid injection. Structured output
is encoded with ASCII separator characters, which real contact data will
essentially never contain, rather than attempting to hand-build JSON in
AppleScript. The separators are generated at runtime via AppleScript's own
`(ASCII character N)`, not embedded as literal bytes in the script source --
Python's str.splitlines() (used to break a script into `-e` arguments)
treats \\x1c/\\x1d/\\x1e as line boundaries and would silently mangle them.
"""
from __future__ import annotations

import asyncio
from typing import Optional

RS = "\x1e"  # record separator: between contacts
GS = "\x1d"  # group separator: between fields of one contact
US = "\x1f"  # unit separator: between items of a multi-value field (phones, etc.)
FS = "\x1c"  # separator between components of a single address

_SEPARATOR_SETUP = '''
    set RS to (ASCII character 30)
    set GS to (ASCII character 29)
    set US to (ASCII character 31)
    set FS to (ASCII character 28)'''

_LABEL_MAP = {
    "home": "_$!<Home>!$_",
    "work": "_$!<Work>!$_",
    "mobile": "_$!<Mobile>!$_",
    "main": "_$!<Main>!$_",
    "other": "_$!<Other>!$_",
}


class ContactsError(Exception):
    pass


def _native_label(label: str) -> str:
    return _LABEL_MAP.get(label.lower(), label)


async def _run_applescript(script: str, argv: list[str]) -> str:
    # NB: split on "\n" explicitly, not str.splitlines() -- splitlines() also
    # breaks on \x1c/\x1d/\x1e, which would silently truncate lines built
    # from AppleScript source that itself contains those bytes.
    args = ["osascript"]
    for line in script.split("\n"):
        args += ["-e", line]
    args += argv
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        stderr_text = stderr.decode().strip()
        if "Invalid index" in stderr_text or "(-1719)" in stderr_text:
            raise ContactsError(
                "Contact not found for the given contact_id. Contacts.app "
                "(especially with iCloud sync enabled) can reassign a "
                "person's id shortly after it's created or edited elsewhere. "
                "Re-resolve the contact via imessage_search_contacts and "
                "retry with the current id."
            )
        raise ContactsError(
            f"Contacts.app automation failed: {stderr_text}. "
            "Check that Automation permission is granted to the process "
            "running this MCP server (System Settings > Privacy & Security "
            "> Automation)."
        )
    return stdout.decode()


def _parse_contact_records(raw: str) -> list[dict]:
    records = []
    for rec in raw.split(RS):
        rec = rec.strip("\n")
        if not rec:
            continue
        fields = rec.split(GS)
        pid, name, phones_blob, emails_blob, addresses_blob = (fields + [""] * 5)[:5]
        phones = []
        for item in phones_blob.split(US):
            if ":" in item:
                label, _, value = item.partition(":")
                phones.append({"label": label, "value": value})
        emails = []
        for item in emails_blob.split(US):
            if ":" in item:
                label, _, value = item.partition(":")
                emails.append({"label": label, "value": value})
        addresses = []
        for item in addresses_blob.split(US):
            parts = item.split(FS)
            if len(parts) == 6:
                label, street, city, state, zip_code, country = parts
                addresses.append({
                    "label": label,
                    "street": street,
                    "city": city,
                    "state": state,
                    "zip": zip_code,
                    "country": country,
                })
        records.append({
            "id": pid,
            "name": name,
            "phones": phones,
            "emails": emails,
            "addresses": addresses,
        })
    return records


_CONTACT_DUMP_SCRIPT = '''on run argv''' + _SEPARATOR_SETUP + '''
    tell application "Contacts"
        set targetPeople to item 1 of argv
        set output to ""
        repeat with p in targetPeople
            set pid to id of p
            set nm to name of p
            set phoneStr to ""
            repeat with ph in phones of p
                if phoneStr is not "" then set phoneStr to phoneStr & US
                set phoneStr to phoneStr & (label of ph) & ":" & (value of ph)
            end repeat
            set emailStr to ""
            repeat with em in emails of p
                if emailStr is not "" then set emailStr to emailStr & US
                set emailStr to emailStr & (label of em) & ":" & (value of em)
            end repeat
            set addrStr to ""
            repeat with a in addresses of p
                if addrStr is not "" then set addrStr to addrStr & US
                set addrStr to addrStr & (label of a) & FS & (street of a) & FS & (city of a) & FS & (state of a) & FS & (zip of a) & FS & (country of a)
            end repeat
            set output to output & pid & GS & nm & GS & phoneStr & GS & emailStr & GS & addrStr & RS
        end repeat
        return output
    end tell
end run
'''


async def search_contacts(query: str, limit: int) -> list[dict]:
    script = _CONTACT_DUMP_SCRIPT.replace(
        "set targetPeople to item 1 of argv",
        "set q to item 1 of argv\n        set targetPeople to (every person whose name contains q)",
    )
    raw = await _run_applescript(script, [query])
    return _parse_contact_records(raw)[:limit]


async def get_contact(contact_id: str) -> Optional[dict]:
    script = _CONTACT_DUMP_SCRIPT.replace(
        "set targetPeople to item 1 of argv",
        "set cid to item 1 of argv\n        set targetPeople to (every person whose id is cid)",
    )
    raw = await _run_applescript(script, [contact_id])
    records = _parse_contact_records(raw)
    return records[0] if records else None


async def create_contact(
    first_name: str,
    last_name: Optional[str],
    phone: Optional[str],
    phone_label: str,
    email: Optional[str],
    email_label: str,
) -> str:
    script = '''on run argv
    set firstName to item 1 of argv
    set lastName to item 2 of argv
    set phoneVal to item 3 of argv
    set phoneLbl to item 4 of argv
    set emailVal to item 5 of argv
    set emailLbl to item 6 of argv
    tell application "Contacts"
        set p to make new person with properties {first name:firstName, last name:lastName}
        if phoneVal is not "" then
            make new phone at end of phones of p with properties {value:phoneVal, label:phoneLbl}
        end if
        if emailVal is not "" then
            make new email at end of emails of p with properties {value:emailVal, label:emailLbl}
        end if
        save
        return id of p
    end tell
end run
'''
    argv = [
        first_name, last_name or "",
        phone or "", _native_label(phone_label),
        email or "", _native_label(email_label),
    ]
    raw = await _run_applescript(script, argv)
    return raw.strip()


async def update_contact_address(
    contact_id: str, street: str, city: str, state: str, zip_code: str, country: str, label: str,
) -> None:
    native_label = _native_label(label)
    script = '''on run argv
    set cid to item 1 of argv
    set streetVal to item 2 of argv
    set cityVal to item 3 of argv
    set stateVal to item 4 of argv
    set zipVal to item 5 of argv
    set countryVal to item 6 of argv
    set lbl to item 7 of argv
    tell application "Contacts"
        set p to first person whose id is cid
        set existing to missing value
        repeat with a in addresses of p
            if (label of a) is lbl then
                set existing to a
                exit repeat
            end if
        end repeat
        if existing is missing value then
            set existing to make new address at end of addresses of p
            set label of existing to lbl
        end if
        set street of existing to streetVal
        set city of existing to cityVal
        set state of existing to stateVal
        set zip of existing to zipVal
        set country of existing to countryVal
        save
    end tell
end run
'''
    argv = [contact_id, street, city, state, zip_code, country, native_label]
    await _run_applescript(script, argv)


async def update_contact_phone(contact_id: str, phone: str, label: str) -> None:
    native_label = _native_label(label)
    script = '''on run argv
    set cid to item 1 of argv
    set phoneVal to item 2 of argv
    set lbl to item 3 of argv
    tell application "Contacts"
        set p to first person whose id is cid
        set existing to missing value
        repeat with ph in phones of p
            if (label of ph) is lbl then
                set existing to ph
                exit repeat
            end if
        end repeat
        if existing is missing value then
            make new phone at end of phones of p with properties {value:phoneVal, label:lbl}
        else
            set value of existing to phoneVal
        end if
        save
    end tell
end run
'''
    argv = [contact_id, phone, native_label]
    await _run_applescript(script, argv)
