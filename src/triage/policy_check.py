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
            spans = set()
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
