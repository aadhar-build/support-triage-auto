"""Print aggregate counts for twcs.csv. Never prints tweet text, rows or author ids.

Usage: python3 -m triage.profile [path]   (default: data/raw/twcs.csv)
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from triage.loader import HeaderError, LoadStats, iter_tweets

DEFAULT_PATH = "data/raw/twcs.csv"


@dataclass
class Profile:
    rows_read: int = 0
    rows_parsed: int = 0
    rows_skipped: int = 0
    skip_reasons: Counter = field(default_factory=Counter)
    inbound_rows: int = 0
    brand_rows: int = 0
    distinct_brands: int = 0
    earliest_created_at: Optional[datetime] = None
    latest_created_at: Optional[datetime] = None


def build_profile(source) -> Profile:
    """Stream the source once and return aggregate counts."""
    stats = LoadStats()
    p = Profile()
    brands = set()
    for t in iter_tweets(source, stats):
        if t.inbound:
            p.inbound_rows += 1
        else:
            p.brand_rows += 1
            brands.add(t.author_id)
        if p.earliest_created_at is None or t.created_at < p.earliest_created_at:
            p.earliest_created_at = t.created_at
        if p.latest_created_at is None or t.created_at > p.latest_created_at:
            p.latest_created_at = t.created_at
    p.rows_read = stats.rows_read
    p.rows_parsed = stats.rows_parsed
    p.rows_skipped = stats.rows_skipped
    p.skip_reasons = Counter(stats.skip_reasons)
    p.distinct_brands = len(brands)
    return p


def _iso(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt is not None else "none"


def format_profile(p: Profile) -> List[str]:
    lines = [
        f"rows_read: {p.rows_read}",
        f"rows_parsed: {p.rows_parsed}",
        f"inbound_rows: {p.inbound_rows}",
        f"brand_rows: {p.brand_rows}",
        f"distinct_brands: {p.distinct_brands}",
        f"earliest_created_at: {_iso(p.earliest_created_at)}",
        f"latest_created_at: {_iso(p.latest_created_at)}",
        f"rows_skipped: {p.rows_skipped}",
    ]
    for reason in sorted(p.skip_reasons):
        if p.skip_reasons[reason] > 0:
            lines.append(f"skipped.{reason}: {p.skip_reasons[reason]}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m triage.profile", description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", default=DEFAULT_PATH)
    args = parser.parse_args(argv)

    try:
        p = build_profile(args.path)
    except FileNotFoundError:
        print(f"file not found: {args.path}", file=sys.stderr)
        print("hint: run scripts/download_data.sh to fetch the dataset", file=sys.stderr)
        return 2
    except HeaderError as e:
        print(f"bad header in {args.path}: {e}", file=sys.stderr)
        return 2
    except UnicodeDecodeError as e:
        print(f"{args.path} is not valid UTF-8 (byte offset {e.start})", file=sys.stderr)
        return 2

    for line in format_profile(p):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
