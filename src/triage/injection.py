"""Notice when a customer tries to give the system orders.

The phrases live in an owner-controlled data file (shape of
policies/injection_phrases.json); nothing here hard-codes a phrase and there is
no default path. load_phrases fails closed: a missing, malformed or empty file
raises PhrasesError, so a blanked file can never quietly turn detection off.

detect looks only at customer turns (brand turns are never passed in). A phrase
hits when its tokens form a consecutive run over back-to-back customer turns,
taking at least one token from every turn it spans, so a customer turn with any
other words (or with no tokens at all) breaks the run. A hit depends only on the
turns it spans, so detect on a prefix never looks ahead.

Phrases and turns are normalised the same way before matching: NFKC, then the
IGNORABLE characters are deleted, then the curly apostrophe becomes a straight
one and the text is lowercased. Tokens are runs of [a-z0-9'] (the cheap tier's
word model). No matched text is returned or logged.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterator, List, Sequence, Tuple

# Invisible characters deleted before matching. Other format characters are kept
# on purpose, because some have real linguistic uses.
IGNORABLE = (
    "\u00ad",  # soft hyphen
    "\u200b",  # zero width space
    "\u200c",  # zero width non-joiner
    "\u200d",  # zero width joiner
    "\u2060",  # word joiner
    "\ufeff",  # zero width no-break space
)

_DELETE = {ord(ch): None for ch in IGNORABLE}
_WORD = re.compile(r"[a-z0-9']+")


class PhrasesError(ValueError):
    """A phrase file is missing or malformed. The message is one line naming the file."""


@dataclass(frozen=True)
class Phrase:
    id: str
    text: str
    tokens: tuple


@dataclass(frozen=True)
class Hit:
    phrase_id: str
    first_turn: int
    last_turn: int


def _tokens(text: str) -> Tuple[str, ...]:
    norm = unicodedata.normalize("NFKC", text).translate(_DELETE)
    norm = norm.replace("\u2019", "'").lower()
    return tuple(_WORD.findall(norm))


def load_phrases(path) -> Tuple[Phrase, ...]:
    """Load and validate a phrase file. Raises PhrasesError with a one-line message."""
    def fail(problem):
        return PhrasesError("bad injection phrases file %s: %s" % (path, problem))

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise PhrasesError("injection phrases file not found: %s" % path)
    except OSError as e:
        raise PhrasesError("cannot read injection phrases file %s: %s"
                           % (path, e.strerror or e))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise fail("not valid UTF-8 (byte offset %d)" % e.start)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise fail("not valid JSON (line %d, column %d)" % (e.lineno, e.colno))
    if not isinstance(data, dict):
        raise fail("top level must be a JSON object")
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise fail("'version' is missing or is not a non-empty string")
    entries = data.get("phrases")
    if not isinstance(entries, list):
        raise fail("'phrases' is missing or is not a list")
    if not entries:
        raise fail("'phrases' is empty; refusing to run with detection off")

    phrases = []
    seen = set()
    for index, entry in enumerate(entries):
        where = "phrase at index %d" % index
        if not isinstance(entry, dict):
            raise fail("%s is not an object" % where)
        phrase_id = entry.get("id")
        if not isinstance(phrase_id, str) or not phrase_id:
            raise fail("%s: 'id' is missing or is not a non-empty string" % where)
        where = "phrase at index %d (id %r)" % (index, phrase_id)
        if phrase_id in seen:
            raise fail("%s: duplicate 'id'" % where)
        seen.add(phrase_id)
        phrase_text = entry.get("text")
        if not isinstance(phrase_text, str) or not phrase_text:
            raise fail("%s: 'text' is missing or is not a non-empty string" % where)
        tokens = _tokens(phrase_text)
        if not tokens:
            raise fail("%s: 'text' gives no tokens" % where)
        phrases.append(Phrase(id=phrase_id, text=phrase_text, tokens=tokens))
    return tuple(phrases)


def _iter_hits(customer_turns: Sequence[str], phrases: Sequence[Phrase]) -> Iterator[Hit]:
    by_first: Dict[str, List[Phrase]] = {}
    for phrase in phrases:
        by_first.setdefault(phrase.tokens[0], []).append(phrase)

    stream: List[str] = []
    turn_of: List[int] = []
    for turn_index, text in enumerate(customer_turns):
        tokens = _tokens(text)
        stream.extend(tokens)
        turn_of.extend([turn_index] * len(tokens))

    total = len(stream)
    for start, token in enumerate(stream):
        for phrase in by_first.get(token, ()):
            end = start + len(phrase.tokens)
            if end > total or tuple(stream[start:end]) != phrase.tokens:
                continue
            # Every turn in the span must give a token: a token-less turn shows
            # up as a jump of more than one turn between neighbouring tokens.
            if any(turn_of[i + 1] - turn_of[i] > 1 for i in range(start, end - 1)):
                continue
            yield Hit(phrase.id, turn_of[start], turn_of[end - 1])


def detect(customer_turns: Sequence[str], phrases: Sequence[Phrase]) -> Tuple[Hit, ...]:
    """Return every phrase hit, de-duplicated and sorted by (first_turn, last_turn, phrase_id).

    customer_turns are the texts of the inbound turns, in order. Turn numbers in
    a Hit are 0-based indices into customer_turns.
    """
    hits = set(_iter_hits(customer_turns, phrases))
    return tuple(sorted(hits, key=lambda h: (h.first_turn, h.last_turn, h.phrase_id)))


def is_flagged(customer_turns: Sequence[str], phrases: Sequence[Phrase]) -> bool:
    """True exactly when detect would return at least one hit. Stops at the first hit."""
    return next(_iter_hits(customer_turns, phrases), None) is not None
