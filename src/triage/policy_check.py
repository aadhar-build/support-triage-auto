"""Check draft public replies against the owner's policy rules file, and match customer
turns against the file's always-escalate topics.

The rules and topics come only from the file given to `load_rules` and
`load_escalate_topics`; this module holds no rule ids, topic ids or phrases of its own.
Standard library only.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


RULE_TYPES = ("banned_phrase", "pii_echo")
DETECTORS = ("card_number", "email_from_customer", "phone_from_customer")


class RulesError(ValueError):
    """The rules file is missing, unreadable or malformed."""


@dataclass(frozen=True)
class Rule:
    id: str
    type: str
    phrases: tuple
    detect: tuple


@dataclass(frozen=True)
class Topic:
    """An always-escalate topic: its id, its phrases and their patterns, compiled once."""
    id: str
    phrases: tuple
    patterns: tuple = field(default=(), repr=False, compare=False)


@dataclass(frozen=True)
class Violation:
    rule_id: str
    start: int
    end: int
    text: str


CURLY_APOSTROPHE = "\u2019"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Stands in for email characters so card and phone detectors skip them; not a digit,
# letter or separator, and one character long so offsets stay the same.
EMAIL_MASK = "\x00"
# A maximal run of digits joined by nothing, one space or one dash.
CARD_RUN = re.compile(r"[0-9](?:[ -]?[0-9])*")
# Optional "+" and "(", then digits joined by at most two of: space - . ( )
PHONE_RUN = re.compile(r"\+?\(?[0-9](?:[ .()-]{0,2}[0-9])*")
TOPIC_ID = re.compile(r"[a-z0-9][a-z0-9-]*")


def _read_rules_object(path):
    """Return (path, fail, data) for a rules file whose top level is a JSON object."""
    path = Path(path)

    def fail(problem):
        return RulesError("%s: %s" % (path, problem))

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise fail("cannot read rules file (%s)" % exc) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise fail("not valid JSON (%s)" % exc) from exc
    if not isinstance(data, dict):
        raise fail("top level must be a JSON object")
    return path, fail, data


def load_rules(path):
    """Read the rules file at `path` and return its public reply rules in file order.

    The result is a non-empty tuple of `Rule` records, so callers get the number of
    loaded rules as `len(load_rules(path))` (for example, to log or display it).

    Raises RulesError, naming the file and the problem, for any missing or malformed
    file. An empty `public_reply_rules` list is refused too (fail closed), so a blanked
    rules file can never make every draft safe to send.
    """
    path, _, data = _read_rules_object(path)
    return rules_from_data(data, path)


def _failer(source):
    def fail(problem):
        return RulesError("%s: %s" % (source, problem))
    return fail


def rules_from_data(data, source):
    """Validate an already-parsed rules object and return its public reply rules.

    Same checks and messages as `load_rules`; `source` names where `data` came from.
    """
    fail = _failer(source)
    if not isinstance(data, dict):
        raise fail("top level must be a JSON object")
    raw_rules = data.get("public_reply_rules")
    if not isinstance(raw_rules, list):
        raise fail("'public_reply_rules' is missing or is not a list")
    if not raw_rules:
        raise fail("'public_reply_rules' is empty; at least one rule is required")

    rules = []
    seen = set()
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise fail("rule at index %d is not an object" % index)
        rule_id = raw.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise fail("rule at index %d has no 'id' or its 'id' is not a non-empty string"
                       % index)
        if rule_id in seen:
            raise fail("duplicate rule id %r" % rule_id)
        seen.add(rule_id)
        rule_type = raw.get("type")
        if rule_type not in RULE_TYPES:
            raise fail("rule %r has type %r; expected one of %s"
                       % (rule_id, rule_type, ", ".join(RULE_TYPES)))
        if rule_type == "banned_phrase":
            phrases = raw.get("phrases")
            if not isinstance(phrases, list) or not phrases:
                raise fail("rule %r needs a non-empty 'phrases' list" % rule_id)
            for phrase in phrases:
                if not isinstance(phrase, str) or not phrase:
                    raise fail("rule %r has a phrase that is not a non-empty string: %r"
                               % (rule_id, phrase))
            rules.append(Rule(rule_id, rule_type, tuple(phrases), ()))
        else:
            detect = raw.get("detect")
            if not isinstance(detect, list) or not detect:
                raise fail("rule %r needs a non-empty 'detect' list" % rule_id)
            for name in detect:
                if name not in DETECTORS:
                    raise fail("rule %r has unknown detector %r; expected one of %s"
                               % (rule_id, name, ", ".join(DETECTORS)))
            rules.append(Rule(rule_id, rule_type, (), tuple(detect)))
    return tuple(rules)


def load_escalate_topics(path):
    """Read the rules file at `path` and return its always-escalate topics in file order.

    Each item of `escalate_always` is an object with a kebab-case `id` and a non-empty
    `phrases` list of non-empty strings; other keys are ignored. Raises RulesError, naming
    the file and the problem, for a missing, empty or malformed list (fail closed), so a
    blanked rules file can never switch the rule off.
    """
    path, _, data = _read_rules_object(path)
    return topics_from_data(data, path)


def topics_from_data(data, source):
    """Validate an already-parsed rules object and return its always-escalate topics.

    Same checks and messages as `load_escalate_topics`; `source` names where `data` came
    from.
    """
    fail = _failer(source)
    if not isinstance(data, dict):
        raise fail("top level must be a JSON object")
    raw_topics = data.get("escalate_always")
    if not isinstance(raw_topics, list):
        raise fail("'escalate_always' is missing or is not a list")
    if not raw_topics:
        raise fail("'escalate_always' is empty; at least one topic is required")
    topics = []
    seen = set()
    for index, raw in enumerate(raw_topics):
        if not isinstance(raw, dict):
            raise fail("topic at index %d is not an object" % index)
        topic_id = raw.get("id")
        if not isinstance(topic_id, str) or not topic_id:
            raise fail("topic at index %d has no 'id' or its 'id' is not a non-empty string"
                       % index)
        if not TOPIC_ID.fullmatch(topic_id):
            raise fail("topic id %r is not kebab-case ([a-z0-9][a-z0-9-]*)" % topic_id)
        if topic_id in seen:
            raise fail("duplicate topic id %r" % topic_id)
        seen.add(topic_id)
        phrases = raw.get("phrases")
        if not isinstance(phrases, list) or not phrases:
            raise fail("topic %r needs a non-empty 'phrases' list" % topic_id)
        for phrase in phrases:
            if not isinstance(phrase, str) or not phrase:
                raise fail("topic %r has a phrase that is not a non-empty string: %r"
                           % (topic_id, phrase))
        topics.append(Topic(topic_id, tuple(phrases),
                            tuple(_phrase_pattern(p) for p in phrases)))
    return tuple(topics)


def rules_version(path) -> Optional[str]:
    """The rules file's top-level `version` string, or None when it has none."""
    _, _, data = _read_rules_object(path)
    version = data.get("version")
    return version if isinstance(version, str) else None


def matched_topics(customer_turns, topics):
    """Ids of the topics with a phrase hit in any customer turn, in topic order, once each.

    Same matching as banned phrases: whole words, any case, a space matches any run of
    whitespace, and curly and straight apostrophes are equal.
    """
    texts = [turn.replace(CURLY_APOSTROPHE, "'") for turn in customer_turns]
    return tuple(topic.id for topic in topics
                 if any(p.search(text) for p in topic.patterns for text in texts))


def check(draft, customer_turns, rules):
    """Return every policy violation in `draft`, sorted by (start, end, rule_id)."""
    hits = set()
    for rule in rules:
        if rule.type == "banned_phrase":
            spans = _phrase_spans(draft, rule.phrases)
        else:
            spans = _pii_spans(draft, customer_turns, rule.detect)
        hits.update((start, end, rule.id) for start, end in spans)
    return [Violation(rule_id, start, end, draft[start:end])
            for start, end, rule_id in sorted(hits)]


def is_safe_to_send(draft, customer_turns, rules):
    """True only when `check` finds no violation at all."""
    return check(draft, customer_turns, rules) == []


def _phrase_spans(draft, phrases):
    # Both apostrophes are one character, so swapping them keeps every offset.
    text = draft.replace(CURLY_APOSTROPHE, "'")
    spans = set()
    for phrase in phrases:
        for match in _phrase_pattern(phrase).finditer(text):
            spans.add(match.span())
    return spans


def _phrase_pattern(phrase):
    """Whole-word, case-insensitive pattern for a phrase; a space matches any whitespace
    run. Callers swap curly apostrophes for straight ones in the text they search."""
    words = phrase.replace(CURLY_APOSTROPHE, "'").split(" ")
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)",
                      re.IGNORECASE)


def _pii_spans(draft, customer_turns, detect):
    spans = set()
    masked = _mask_emails(draft)
    if "card_number" in detect:
        spans |= _card_spans(masked)
    if "phone_from_customer" in detect:
        posted = {key for turn in customer_turns for _, key in _phones(_mask_emails(turn))}
        spans |= {span for span, key in _phones(masked) if key in posted}
    if "email_from_customer" in detect:
        posted = {m.group().lower() for turn in customer_turns for m in EMAIL.finditer(turn)}
        spans |= {m.span() for m in EMAIL.finditer(draft) if m.group().lower() in posted}
    return spans


def _mask_emails(text):
    return EMAIL.sub(lambda m: EMAIL_MASK * len(m.group()), text)


def _card_spans(text):
    spans = set()
    for match in CARD_RUN.finditer(text):
        start, end = match.span()
        if start > 0 and text[start - 1].isalnum():
            continue
        if end < len(text) and text[end].isalnum():
            continue
        digits = [int(ch) for ch in match.group() if ch.isdigit()]
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            spans.add((start, end))
    return spans


def _phones(text):
    """Yield (span, key) for each phone candidate; the key is its digits only."""
    for match in PHONE_RUN.finditer(text):
        key = "".join(ch for ch in match.group() if ch.isdigit())
        if 7 <= len(key) <= 15:
            yield match.span(), key


def _luhn_ok(digits):
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0
