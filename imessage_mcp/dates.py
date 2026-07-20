"""Conversions between Apple's Core Data timestamp epoch and Unix/ISO time.

macOS Messages (chat.db) stores dates as nanoseconds since 2001-01-01T00:00:00Z
on modern macOS (10.13+). This module assumes that format.
"""
from __future__ import annotations

from datetime import datetime, timezone

APPLE_EPOCH_OFFSET = 978307200  # unix seconds at 2001-01-01T00:00:00Z


def apple_ns_to_unix(apple_ns: int) -> float:
    return apple_ns / 1e9 + APPLE_EPOCH_OFFSET


def apple_ns_to_iso(apple_ns: int) -> str:
    ts = apple_ns_to_unix(apple_ns)
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def unix_to_apple_ns(unix_ts: float) -> int:
    return int((unix_ts - APPLE_EPOCH_OFFSET) * 1e9)


def now_apple_ns() -> int:
    return unix_to_apple_ns(datetime.now(tz=timezone.utc).timestamp())


def iso_to_apple_ns(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return unix_to_apple_ns(dt.timestamp())
