"""Streaming loader for the Customer Support on Twitter file (twcs.csv).

Standard library only. Rows are parsed one at a time; bad rows are skipped and
counted by reason instead of raising.
"""

from __future__ import annotations

import csv
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional, Tuple

EXPECTED_COLUMNS = (
    "tweet_id",
    "author_id",
    "inbound",
    "created_at",
    "text",
    "response_tweet_id",
    "in_response_to_tweet_id",
)


class HeaderError(ValueError):
    """The CSV header is empty or lacks an expected column."""


@dataclass(frozen=True)
class Tweet:
    tweet_id: int
    author_id: str
    inbound: bool
    created_at: datetime
    text: str
    response_tweet_ids: Tuple[int, ...]
    in_response_to_tweet_id: Optional[int]


@dataclass
class LoadStats:
    rows_read: int = 0
    rows_parsed: int = 0
    rows_skipped: int = 0
    skip_reasons: Counter = field(default_factory=Counter)


class _Skip(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_ID_RE = re.compile(r"(\d+)(?:\.0+)?", re.ASCII)

# Twitter format, e.g. "Tue Oct 31 22:10:47 +0000 2017". Parsed by hand so the
# result never depends on the process locale.
_DATE_RE = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"(\d{1,2}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2}) (\d{4})",
    re.ASCII,
)
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}


def _parse_id(value: str) -> Optional[int]:
    """Return the id, or None if the value is not a valid id."""
    m = _ID_RE.fullmatch(value.strip())
    return int(m.group(1)) if m else None


def _parse_date(value: str) -> Optional[datetime]:
    m = _DATE_RE.fullmatch(value.strip())
    if not m:
        return None
    _, mon, day, hh, mm, ss, sign, tzh, tzm, year = m.groups()
    if int(tzh) > 23 or int(tzm) > 59:
        return None
    offset = timedelta(hours=int(tzh), minutes=int(tzm))
    if sign == "-":
        offset = -offset
    try:
        return datetime(int(year), _MONTHS[mon], int(day), int(hh), int(mm), int(ss),
                        tzinfo=timezone(offset))
    except ValueError:
        return None


def _parse_row(row: dict) -> Tweet:
    if None in row or any(v is None for v in row.values()):
        raise _Skip("field_count")

    tweet_id = _parse_id(row["tweet_id"])
    if tweet_id is None:
        raise _Skip("tweet_id")

    author_id = row["author_id"]
    if not author_id.strip():
        raise _Skip("author_id")

    inbound_raw = row["inbound"].strip().lower()
    if inbound_raw not in ("true", "false"):
        raise _Skip("inbound")
    inbound = inbound_raw == "true"

    created_at = _parse_date(row["created_at"])
    if created_at is None:
        raise _Skip("created_at")

    response_raw = row["response_tweet_id"]
    response_ids = []
    if response_raw.strip():
        for piece in response_raw.split(","):
            rid = _parse_id(piece)
            if rid is None:
                raise _Skip("response_tweet_id")
            response_ids.append(rid)

    in_reply_raw = row["in_response_to_tweet_id"]
    in_reply: Optional[int] = None
    if in_reply_raw.strip():
        in_reply = _parse_id(in_reply_raw)
        if in_reply is None:
            raise _Skip("in_response_to_tweet_id")

    return Tweet(
        tweet_id=tweet_id,
        author_id=author_id,
        inbound=inbound,
        created_at=created_at,
        text=row["text"],
        response_tweet_ids=tuple(response_ids),
        in_response_to_tweet_id=in_reply,
    )


def _check_header(fieldnames) -> None:
    if not fieldnames:
        raise HeaderError("empty file or header: expected columns " + ", ".join(EXPECTED_COLUMNS))
    missing = [c for c in EXPECTED_COLUMNS if c not in fieldnames]
    if missing:
        raise HeaderError("header is missing columns: " + ", ".join(missing))


def _iter_file(f, stats: LoadStats) -> Iterator[Tweet]:
    reader = csv.DictReader(f)
    _check_header(reader.fieldnames)
    for row in reader:
        stats.rows_read += 1
        try:
            tweet = _parse_row(row)
        except _Skip as skip:
            stats.rows_skipped += 1
            stats.skip_reasons[skip.reason] += 1
            continue
        stats.rows_parsed += 1
        yield tweet


def iter_tweets(source, stats: Optional[LoadStats] = None) -> Iterator[Tweet]:
    """Yield Tweet records from a path or an open text file, one row at a time.

    HeaderError is raised on the first next() if the header is bad. If stats is
    given it is updated in place as rows stream.
    """
    if stats is None:
        stats = LoadStats()
    if isinstance(source, (str, os.PathLike)):
        with open(source, newline="", encoding="utf-8") as f:
            yield from _iter_file(f, stats)
    else:
        yield from _iter_file(source, stats)
