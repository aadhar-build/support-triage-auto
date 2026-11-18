"""Per-brand rules: read the one fenced ```rules JSON block in brands/<brand>/guidelines.md.

A brand file is free Markdown plus exactly one block whose opening line is exactly
```rules (after trailing whitespace is stripped). Only that block is parsed; prose and
other fenced blocks never change behaviour. Loading fails closed: a missing, duplicated,
malformed, empty or invalid block raises RulesError naming the file.

A JSON rules file (`load_effective`) and a brand file (`load_brand_file`) go through the
same validators and give the same `EffectiveRules`.

`python3 -m triage.brand_rules --conformance` checks a brand file against a JSON rules
file and prints only the names of differing top-level keys. Standard library only.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from triage.context_signals import RepeatContactError, RepeatContactRule, \
    repeat_contact_from_data
from triage.policy_check import RulesError, _read_rules_object, rules_from_data, \
    topics_from_data
from triage.trace import REPO_ROOT

BRANDS_DIR = REPO_ROOT / "brands"
BRAND_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
FENCE_OPEN = "```rules"
FENCE_CLOSE = "```"


@dataclass(frozen=True)
class EffectiveRules:
    rules: tuple
    topics: tuple
    repeat_contact: Optional[RepeatContactRule]
    version: Optional[str]
    status: Optional[str]


def effective_from_data(data, source) -> EffectiveRules:
    """Validate a parsed rules object and return its effective configuration."""
    rules = rules_from_data(data, source)
    topics = topics_from_data(data, source)
    repeat = None
    if "repeat_contact" in data:
        try:
            repeat = repeat_contact_from_data(data, source)
        except RepeatContactError as exc:
            raise RulesError(str(exc)) from exc
    version = data.get("version")
    status = data.get("status")
    return EffectiveRules(rules, topics, repeat,
                          version if isinstance(version, str) else None,
                          status if isinstance(status, str) else None)


def load_effective(path) -> EffectiveRules:
    """Read a JSON rules file and return its effective configuration."""
    path, _, data = _read_rules_object(path)
    return effective_from_data(data, path)


def _rules_block(path):
    """Return (block text, line number of the opening fence) for the brand file."""
    path = Path(path)

    def fail(problem):
        return RulesError("%s: %s" % (path, problem))

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise fail("cannot read brand file (%s)" % exc) from exc
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith(FENCE_OPEN):
            if line != FENCE_OPEN:
                raise fail("malformed rules fence on line %d; it must be exactly %s"
                           % (i + 1, FENCE_OPEN))
            start = i + 1
            end = start
            while end < len(lines) and lines[end].rstrip() != FENCE_CLOSE:
                end += 1
            if end == len(lines):
                raise fail("unclosed ```rules block starting on line %d" % (i + 1))
            blocks.append(("\n".join(lines[start:end]), i + 1))
            i = end + 1
            continue
        i += 1
    if not blocks:
        raise fail("no ```rules block found")
    if len(blocks) > 1:
        raise fail("more than one ```rules block found (%d); exactly one is allowed"
                   % len(blocks))
    return path, fail, blocks[0][0]


def _brand_object(path):
    path, fail, block = _rules_block(path)
    if not block.strip():
        raise fail("the ```rules block is empty")
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        raise fail("rules block is not valid JSON (line %d of the block: %s)"
                   % (exc.lineno, exc.msg)) from exc
    if not isinstance(data, dict):
        raise fail("rules block top level must be a JSON object")
    return path, data


def load_brand_file(path) -> EffectiveRules:
    """Read a brand guidelines file and return its effective configuration."""
    path, data = _brand_object(path)
    return effective_from_data(data, path)


def brand_path(name, brands_dir=BRANDS_DIR) -> Path:
    """The resolved guidelines.md for brand `name`, refused unless it lies inside
    `brands_dir` with symlinks followed. The name is checked before any filesystem access."""
    if not isinstance(name, str) or not BRAND_NAME.fullmatch(name):
        raise RulesError("bad brand name %r; expected [a-z0-9][a-z0-9_-]{0,63}" % (name,))
    root = Path(brands_dir).resolve()
    resolved = (root / name / "guidelines.md").resolve()
    if root not in resolved.parents:
        raise RulesError("brand %r resolves outside %s" % (name, root))
    if not resolved.is_file():
        raise RulesError("brand '%s' not found under %s" % (name, root))
    return resolved


def add_rules_args(parser, default_rules):
    """Add --rules, --brand and --brands-dir to a runner's parser."""
    parser.add_argument("--rules", type=Path, default=None,
                        help="JSON policy rules file (default: %s)" % default_rules)
    parser.add_argument("--brand", default=None,
                        help="brand whose brands/<brand>/guidelines.md rules block to use")
    parser.add_argument("--brands-dir", type=Path, default=BRANDS_DIR,
                        help="test seam: the brands directory (default: the repo's brands/)")
    parser.set_defaults(_default_rules=default_rules)


def load_rules_source(args):
    """Return (label, EffectiveRules) from --brand or --rules. Raises RulesError."""
    if args.brand is not None and args.rules is not None:
        raise RulesError("use either --brand or --rules, not both")
    if args.brand is not None:
        return args.brand, load_brand_file(brand_path(args.brand, args.brands_dir))
    path = args.rules if args.rules is not None else args._default_rules
    return "none", load_effective(path)


def rules_path_of(args):
    """The file the rules came from, for messages."""
    if args.brand is not None:
        return brand_path(args.brand, args.brands_dir)
    return args.rules if args.rules is not None else args._default_rules


def loaded_fields(label, effective) -> str:
    """The shared stdout fragment: brand, rules and topics loaded, repeat_contact on/off."""
    return "brand=%s rules_loaded=%d topics_loaded=%d repeat_contact=%s" % (
        label, len(effective.rules), len(effective.topics),
        "on" if effective.repeat_contact is not None else "off")


def repeat_contact_token(effective) -> str:
    return "repeat_contact=%s" % ("on" if effective.repeat_contact is not None else "off")


def conformance(brand, rules_path, brands_dir):
    """Return (exit code, message) comparing a brand file with a JSON rules file."""
    try:
        _, brand_data = _brand_object(brand_path(brand, brands_dir))
        _, _, json_data = _read_rules_object(rules_path)
        brand_eff = effective_from_data(brand_data, brand)
        json_eff = effective_from_data(json_data, rules_path)
    except RulesError as exc:
        return 2, "error: %s" % exc
    keys = sorted(k for k in set(brand_data) | set(json_data)
                  if brand_data.get(k, KeyError) != json_data.get(k, KeyError))
    if keys or brand_eff != json_eff:
        return 1, "conformance: differ brand=%s keys=%s" % (brand, ",".join(keys) or "-")
    return 0, "conformance: ok brand=%s" % brand


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m triage.brand_rules")
    parser.add_argument("--conformance", action="store_true", required=True,
                        help="compare a brand file's rules block with a JSON rules file")
    parser.add_argument("--brand", default="amazonhelp")
    parser.add_argument("--rules", type=Path, default=REPO_ROOT / "policies" / "rules.json")
    parser.add_argument("--brands-dir", type=Path, default=BRANDS_DIR)
    args = parser.parse_args(argv)
    code, message = conformance(args.brand, args.rules, args.brands_dir)
    print(message, file=sys.stderr if code == 2 else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
