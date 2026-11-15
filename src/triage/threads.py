"""Rebuild conversation threads from the Tweet records of triage.loader.

One thread per customer (inbound) tweet that has no parent. Broken links, loops,
brand-started conversations, duplicate ids and parent-link disagreements are
counted in ThreadStats and never raise.

Memory: the build makes one pass over the tweets and holds one Tweet record
(text included) for every unique parsed row at once. It also holds an
id-to-parent map, a child-to-listers map built from response_tweet_ids, and a
per-tweet status map. The returned threads hold one Turn per placed tweet.
Memory therefore grows linear in the row count (about 2.8M rows for the full
file). No byte figure has been measured.

Parent-chain walks are iterative and memoised, so deep threads cannot hit the
recursion limit, loops cannot spin forever, and total work is linear in the
number of tweets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

from triage.loader import LoadStats, Tweet, iter_tweets


@dataclass(frozen=True)
class Turn:
    tweet_id: int
    author_id: str
    inbound: bool
    created_at: datetime
    text: str


@dataclass(frozen=True)
class Thread:
    root_tweet_id: int
    brand_ids: Tuple[str, ...]
    turns: Tuple[Turn, ...]
    brand_replied: bool


@dataclass
class ThreadStats:
    tweets_seen: int = 0
    duplicate_ids: int = 0
    threads_built: int = 0
    tweets_placed: int = 0
    tweets_not_placed: int = 0
    broken_links: int = 0
    cycles: int = 0
    cycle_tweets: int = 0
    brand_started: int = 0
    parent_link_disagreements: int = 0
    response_id_fallbacks: int = 0
    load: Optional[LoadStats] = None


def _sort_key(t) -> Tuple[datetime, int]:
    return (t.created_at, t.tweet_id)


def _resolve_parents(kept: Dict[int, Tweet], stats: ThreadStats) -> Dict[int, Optional[int]]:
    listers: Dict[int, Set[int]] = {}
    for t in kept.values():
        for child in t.response_tweet_ids:
            if child != t.tweet_id:
                listers.setdefault(child, set()).add(t.tweet_id)

    parent: Dict[int, Optional[int]] = {}
    for tid, t in kept.items():
        who = listers.get(tid, ())
        if t.in_response_to_tweet_id is not None:
            p = t.in_response_to_tweet_id
            if any(lister != p for lister in who):
                stats.parent_link_disagreements += 1
            parent[tid] = p
        elif who:
            stats.response_id_fallbacks += 1
            if len(who) > 1:
                stats.parent_link_disagreements += 1
            parent[tid] = min(who)
        else:
            parent[tid] = None
    return parent


def _classify(kept: Dict[int, Tweet], parent: Dict[int, Optional[int]],
              stats: ThreadStats) -> Dict[int, Optional[int]]:
    """Map each tweet id to its thread root id, or None if it is not placed."""
    status: Dict[int, Optional[int]] = {}
    for start in kept:
        if start in status:
            continue
        path: List[int] = []
        on_path: Dict[int, int] = {}
        cur = start
        result: Optional[int] = None
        while True:
            if cur in status:
                result = status[cur]
                break
            if cur in on_path:
                loop = path[on_path[cur]:]
                stats.cycles += 1
                stats.cycle_tweets += len(loop)
                for tid in loop:
                    status[tid] = None
                del path[on_path[cur]:]
                result = None
                break
            on_path[cur] = len(path)
            path.append(cur)
            p = parent[cur]
            if p is None:
                if kept[cur].inbound:
                    result = cur
                else:
                    stats.brand_started += 1
                    result = None
                break
            if p not in kept:
                stats.broken_links += 1
                result = None
                break
            cur = p
        for tid in path:
            status[tid] = result
    return status


def threads_from_tweets(tweets: Iterable[Tweet]) -> Tuple[List[Thread], ThreadStats]:
    """Group tweets into threads. Iterates `tweets` exactly once."""
    stats = ThreadStats()
    kept: Dict[int, Tweet] = {}
    for t in tweets:
        if t.tweet_id in kept:
            stats.duplicate_ids += 1
            continue
        kept[t.tweet_id] = t
    stats.tweets_seen = len(kept)

    parent = _resolve_parents(kept, stats)
    status = _classify(kept, parent, stats)

    groups: Dict[int, List[Tweet]] = {}
    for tid, root in status.items():
        if root is not None:
            groups.setdefault(root, []).append(kept[tid])

    threads: List[Thread] = []
    for root in sorted(groups, key=lambda r: _sort_key(kept[r])):
        members = sorted(groups[root], key=_sort_key)
        turns = tuple(Turn(t.tweet_id, t.author_id, t.inbound, t.created_at, t.text)
                      for t in members)
        brand_ids = tuple(sorted({t.author_id for t in members if not t.inbound}))
        threads.append(Thread(root_tweet_id=root, brand_ids=brand_ids, turns=turns,
                              brand_replied=len(brand_ids) > 0))

    stats.threads_built = len(threads)
    stats.tweets_placed = sum(len(t.turns) for t in threads)
    stats.tweets_not_placed = stats.tweets_seen - stats.tweets_placed
    return threads, stats


def build_threads(source) -> Tuple[List[Thread], ThreadStats]:
    """Load tweets from a path or open text file and group them into threads.

    Loader errors (HeaderError, FileNotFoundError, ...) pass through. A caller's
    open file is not closed.
    """
    load_stats = LoadStats()
    threads, stats = threads_from_tweets(iter_tweets(source, load_stats))
    stats.load = load_stats
    return threads, stats
