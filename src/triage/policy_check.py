"""Check draft public replies against the owner's policy rules file.

The rules come only from the file given to `load_rules`; this module holds no rule ids
or phrases of its own. Standard library only.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path


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
class Violation:
    rule_id: str
    start: int
    end: int
    text: str


CURLY_APOSTROPHE = "\u2019"
# A maximal run of digits joined by nothing, one space or one dash.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Stands in for email characters so card and phone detectors skip them; not a digit,
# letter or separator, and one character long so offsets stay the same.
EMAIL_MASK = "\x00"
CARD_RUN = re.compile(r"[0-9](?:[ -]?[0-9])*")
# Optional "+" and "(", then digits joined by at most two of: space - . ( )
PHONE_RUN = re.compile(r"\+?\(?[0-9](?:[ .()-]{0,2}[0-9])*")


def load_rules(path):
    """Read the rules file at `path` and return its public reply rules in file order.

    Raises RulesError, naming the file and the problem, for any missing or malformed file.
    """
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
    raw_rules = data.get("public_reply_rules")
    if not isinstance(raw_rules, list):
        raise fail("'public_reply_rules' is missing or is not a list")

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
        words = phrase.replace(CURLY_APOSTROPHE, "'").split(" ")
        pattern = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)"
        for match in re.finditer(pattern, text, re.IGNORECASE):
            spans.add(match.span())
    return spans


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
