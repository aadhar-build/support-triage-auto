"""Propose a policy-rules or routing-threshold change, preview it, and open a draft PR.

Usage: python3 -m triage.propose_change --repo DIR --cases CASES.json
                                        [--rules NEW_RULES.json] [--threshold X] [--dry-run]

The owner runs this on demand at a terminal; nothing runs always-on.

1. Validate: the proposed rules file goes through brand_rules.load_effective (reply rules,
   escalate_always topics, repeat_contact); the threshold must be a number in [0, 1]. A bad
   proposal, or one that changes nothing, exits 2 with one line and no git call.
2. Preview: run the synthetic cases under the current and the proposed setting and print a
   before/after table, labelled "path and cost only" (the tiers are fakes). Then a brand
   conformance line against <repo>/brands/amazonhelp/guidelines.md (a warning, not a gate).
3. --dry-run stops here. Otherwise, after the owner types `yes` at a terminal, a
   ChangeClient commits the changed owner file(s) on a new factory/policy-change-<UTC>
   branch in a separate temporary worktree at a captured origin/main commit, checks the
   commit (check_push_ready), pushes that branch only and opens a draft PR. It never
   merges, approves, force-pushes or pushes main. Exit 0 on success, 2 on any refusal.

Output holds rule and topic ids, labels and counts only, never turn, draft or phrase text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from triage import metrics as metrics_mod
from triage.brand_rules import conformance, load_effective
from triage.policy_check import RulesError
from triage.router import DEFAULT_CONFIDENCE_THRESHOLD, ESCALATE_ALWAYS, REPEAT_CONTACT
from triage.run_synthetic import CasesError, _summary, load_cases, run_cases
from triage.tiers import PLACEHOLDER_PRICES

RULES_PATH = "policies/rules.json"
THRESHOLDS_PATH = "evals/thresholds.json"
ALLOWED_PATHS = (RULES_PATH, THRESHOLDS_PATH)
BRAND = "amazonhelp"
BRAND_FILE = "brands/%s/guidelines.md" % BRAND
BRANCH_PREFIX = "factory/policy-change-"
BRANCH_RE = re.compile(r"factory/policy-change-\d{8}T\d{6}Z")
LABEL = "path and cost only"
THRESHOLD_NOTE = ("Threshold change: recorded experiment, not operational. The router still "
                  "uses its code constant until it reads `evals/thresholds.json`.")
FORCED_CODES = (ESCALATE_ALWAYS, REPEAT_CONTACT)
REPEAT_FIELDS = ("window_days", "min_conversations", "effect", "report_window_hours")
NOT_A_TERMINAL = "proposals need the owner at a terminal; use --dry-run"


class ChangeError(Exception):
    """A refused or failed change step. The message is one line."""


# ---------------------------------------------------------------- the git/GitHub boundary

class ChangeClient(ABC):
    """Every git and GitHub action the command takes. There is deliberately no merge,
    approve or auto-merge operation."""

    @abstractmethod
    def capture_base(self) -> str:
        """Fetch origin main once and return its commit SHA."""

    @abstractmethod
    def add_worktree(self, branch: str, base_sha: str) -> Path:
        """Create a new temporary worktree on the new `branch` at `base_sha`, outside the
        owner's checkout; refuse if the branch exists locally or on origin. Returns its path."""

    @abstractmethod
    def commit(self, paths, message: str) -> None:
        """Commit exactly `paths` in the worktree."""

    @abstractmethod
    def verify(self, branch: str, base_sha: str, paths) -> None:
        """Raise ChangeError unless the worktree's commit passes check_push_ready."""

    @abstractmethod
    def push(self, branch: str) -> None:
        """Push `branch` (never main, never force) to origin with an explicit refspec."""

    @abstractmethod
    def create_draft_pr(self, branch: str, base: str, title: str, body: str) -> str:
        """Open a draft PR from `branch` into `base`; return its URL."""

    @abstractmethod
    def remove_worktree(self) -> None:
        """Remove the temporary worktree."""


def check_push_ready(head_ref, parents, changed_paths, base_sha, branch, written_paths) -> None:
    """The pure push guard. Raises ChangeError unless HEAD is exactly the new policy-change
    branch, its only parent is the captured base, and the changed paths equal the written
    paths, all within policies/rules.json and evals/thresholds.json."""
    if not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch):
        raise ChangeError("refusing to push: %r is not a factory/policy-change-<UTC> branch"
                          % (branch,))
    if head_ref != "refs/heads/" + branch:
        raise ChangeError("refusing to push: HEAD is %r, not refs/heads/%s" % (head_ref, branch))
    if list(parents) != [base_sha]:
        raise ChangeError("refusing to push: HEAD's parents %s are not exactly the captured "
                          "base %s" % (",".join(parents) or "-", base_sha))
    written = set(written_paths)
    outside = sorted(written - set(ALLOWED_PATHS))
    if not written or outside:
        raise ChangeError("refusing to push: written paths outside %s: %s"
                          % (", ".join(ALLOWED_PATHS), ",".join(outside) or "-"))
    if set(changed_paths) != written:
        raise ChangeError("refusing to push: changed paths %s differ from written paths %s"
                          % (",".join(sorted(changed_paths)), ",".join(sorted(written))))


class GitGhClient(ChangeClient):
    """The real client: `git` and `gh` with argument lists, never a shell. Constructing it
    runs nothing."""

    def __init__(self, repo, worktree=None):
        self.repo = Path(repo)
        self.worktree = Path(worktree) if worktree is not None else None
        self._tmp_parent = None

    def _run(self, argv, cwd, check=True):
        try:
            return subprocess.run(argv, cwd=str(cwd), check=check, capture_output=True,
                                  text=True)
        except subprocess.CalledProcessError as e:
            detail = " ".join((e.stderr or "").split())[:200]
            raise ChangeError("%s failed (exit %d): %s" % (" ".join(argv[:3]), e.returncode,
                                                           detail or "no output"))
        except OSError as e:
            raise ChangeError("cannot run %s: %s" % (argv[0], e.strerror or e))

    def capture_base(self) -> str:
        self._run(["git", "fetch", "origin", "main"], self.repo)
        sha = self._run(["git", "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
                        self.repo).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ChangeError("cannot read the fetched origin main commit")
        return sha

    def _branch_exists(self, branch) -> bool:
        local = self._run(["git", "rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
                          self.repo, check=False)
        if local.returncode == 0:
            return True
        remote = self._run(["git", "ls-remote", "--heads", "origin", "refs/heads/" + branch],
                           self.repo)
        return bool(remote.stdout.strip())

    def add_worktree(self, branch, base_sha) -> Path:
        if not BRANCH_RE.fullmatch(branch):
            raise ChangeError("refusing branch %r" % (branch,))
        if self._branch_exists(branch):
            raise ChangeError("branch %s already exists locally or on origin" % branch)
        self._tmp_parent = Path(tempfile.mkdtemp(prefix="policy-change-"))
        path = self._tmp_parent / "worktree"
        self._run(["git", "worktree", "add", "-b", branch, str(path), base_sha], self.repo)
        self.worktree = path
        return path

    def commit(self, paths, message) -> None:
        paths = list(paths)
        self._run(["git", "add", "--"] + paths, self.worktree)
        self._run(["git", "commit", "-q", "-m", message, "--"] + paths, self.worktree)

    def verify(self, branch, base_sha, paths) -> None:
        wt = self.worktree
        head_ref = self._run(["git", "symbolic-ref", "HEAD"], wt).stdout.strip()
        parents = self._run(["git", "rev-list", "--parents", "-n", "1", "HEAD"],
                            wt).stdout.split()[1:]
        changed = self._run(["git", "diff", "--name-only", base_sha, "HEAD"],
                            wt).stdout.split("\n")
        check_push_ready(head_ref, parents, [p for p in changed if p], base_sha, branch,
                         list(paths))

    def push(self, branch) -> None:
        if not BRANCH_RE.fullmatch(branch or ""):
            raise ChangeError("refusing to push %r" % (branch,))
        ref = "refs/heads/" + branch
        self._run(["git", "push", "origin", "%s:%s" % (ref, ref)], self.worktree)

    def create_draft_pr(self, branch, base, title, body) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8",
                                         delete=False) as f:
            f.write(body)
            body_file = f.name
        try:
            out = self._run(["gh", "pr", "create", "--draft", "--base", base, "--head", branch,
                             "--title", title, "--body-file", body_file], self.worktree)
        finally:
            Path(body_file).unlink(missing_ok=True)
        return out.stdout.strip()

    def remove_worktree(self) -> None:
        if self.worktree is None or self._tmp_parent is None:
            return
        self._run(["git", "worktree", "remove", str(self.worktree)], self.repo)
        try:
            self._tmp_parent.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------- thresholds file

def _read_thresholds(repo) -> Optional[dict]:
    path = Path(repo) / THRESHOLDS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ChangeError("cannot read %s: %s" % (path, e))
    return data if isinstance(data, dict) else None


def _file_value(data):
    block = data.get("confidence_threshold") if isinstance(data, dict) else None
    value = block.get("value") if isinstance(block, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def current_threshold(repo) -> tuple:
    """(threshold in effect, the file's numeric value or None). The router's code default
    stands in when the file, the key or a numeric value is absent."""
    value = _file_value(_read_thresholds(repo))
    return (DEFAULT_CONFIDENCE_THRESHOLD if value is None else value), value


def write_threshold(path, value) -> None:
    """Set only confidence_threshold.value; every other key keeps its value and order."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("confidence_threshold"), dict):
        raise ChangeError("%s has no confidence_threshold object" % path)
    data["confidence_threshold"]["value"] = value
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_threshold(text):
    """The threshold from the command line, or ChangeError with the one-line message."""
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        value = text
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or not (0.0 <= value <= 1.0):
        raise ChangeError("threshold must be a number between 0 and 1, got %s" % (text,))
    return value


# ---------------------------------------------------------------- preview

def preview(cases, effective, threshold, tiers=None, *, run_at=None,
            gold_revision=None) -> dict:
    """The preview metrics for one rule set and threshold on validated synthetic cases."""
    routed, results = run_cases(cases, effective, threshold=threshold, tiers=tiers)
    if run_at is None:
        run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if gold_revision is None:
        gold_revision = "sha256:" + hashlib.sha256(
            json.dumps(cases, sort_keys=True).encode("utf-8")).hexdigest()
    m = metrics_mod.compute(
        metrics_mod.cases_from_synthetic(results), "synthetic", run_at=run_at,
        cohort={"name": "synthetic", "split": None, "gold_revision": gold_revision,
                "brand_population": "all"},
        sample={"size": None, "seed": None}, config={"threshold": threshold, "prices": None})
    n = m["n"]
    labels = m["action"]["confusion_matrix"]["labels"]
    counts = m["action"]["confusion_matrix"]["counts"]
    handled = sum(counts[i][i] for i, a in enumerate(labels) if a != "escalate")
    esc = m["escalation"]
    if tiers is None:
        from triage.cheap_tier import CheapTier
        from triage.tiers import FakeAgentTier, FakeLargerTier
        tiers = (CheapTier(), FakeLargerTier(), FakeAgentTier())
    prices = {t.name: t.price_per_call for t in tiers}
    total = sum(prices[name] for r in routed for name in r.tiers)
    summary = _summary(results, len(effective.rules), len(effective.topics))
    return {
        "n": n,
        "north_star": {"num": handled, "n": n},
        "missed_escalations": esc["counts"]["fn"],
        "escalation_tpr": {"num": esc["tpr"]["num"], "n": esc["tpr"]["n"]},
        "escalation_tnr": {"num": esc["tnr"]["num"], "n": esc["tnr"]["n"]},
        "forced_escalations": {code: sum(code in r["reason_codes"] for r in results)
                               for code in FORCED_CODES},
        "policy_violations": summary["policy_violations"],
        "policy_mismatches": summary["policy_mismatches"],
        "tier_shares": {name: metrics_mod.proportion(sum(name in r.tiers for r in routed), n)
                        for name in prices},
        "cost_per_1000_usd": 1000 * total / n if n else None,
        "prices_are_placeholders": prices == PLACEHOLDER_PRICES,
    }


def _frac(p) -> str:
    return "%d/%d" % (p["num"], p["n"])


def preview_table(before: dict, after: dict) -> str:
    def cost(p):
        return "n/a" if p["cost_per_1000_usd"] is None else "%.4f" % p["cost_per_1000_usd"]

    rows = [("north_star", _frac(before["north_star"]), _frac(after["north_star"])),
            ("missed_escalations", before["missed_escalations"], after["missed_escalations"]),
            ("escalation_tpr", _frac(before["escalation_tpr"]), _frac(after["escalation_tpr"])),
            ("escalation_tnr", _frac(before["escalation_tnr"]), _frac(after["escalation_tnr"]))]
    rows += [("forced_escalations.%s" % c, before["forced_escalations"][c],
              after["forced_escalations"][c]) for c in FORCED_CODES]
    rows += [(k, before[k], after[k]) for k in ("policy_violations", "policy_mismatches")]
    for name in before["tier_shares"]:
        rows.append(("tier_share.%s" % name, _frac(before["tier_shares"][name]),
                     _frac(after["tier_shares"].get(name, {"num": 0, "n": after["n"]}))))
    cost_name = "cost_per_1000_usd" + (" (placeholder prices)"
                                       if before["prices_are_placeholders"] else "")
    rows.append((cost_name, cost(before), cost(after)))
    lines = ["Preview (synthetic cases), %s. Tiers are offline fakes; this is not a quality "
             "result. north_star = correctly handled without a person (docs/prd.md)." % LABEL,
             "", "| metric | before | after |", "| --- | --- | --- |"]
    lines += ["| %s | %s | %s |" % row for row in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------- what changed

def _by_id(items):
    return {i.get("id"): i for i in items if isinstance(i, dict)} \
        if isinstance(items, list) else {}


def _fmt(value) -> str:
    return "null" if value is None else str(value)


def what_changed(old: dict, new: dict, threshold_change) -> list:
    """Lines naming ids and numeric fields only, never phrase or description text."""
    lines = []
    if threshold_change is not None:
        (old_file, old_eff), new_value = threshold_change
        old_text = _fmt(old_file) if old_file is not None else \
            "null (code default %s)" % old_eff
        lines.append("threshold: %s -> %s" % (old_text, new_value))
    for section in ("public_reply_rules", "escalate_always"):
        a, b = _by_id(old.get(section)), _by_id(new.get(section))
        for word, ids in (("added", sorted(set(b) - set(a))),
                          ("removed", sorted(set(a) - set(b))),
                          ("changed", sorted(i for i in set(a) & set(b) if a[i] != b[i]))):
            if ids:
                lines.append("%s %s: %s" % (section, word, ", ".join(map(str, ids))))
    ra, rb = old.get("repeat_contact"), new.get("repeat_contact")
    if isinstance(ra, dict) and rb is None:
        lines.append("repeat_contact: removed")
    elif ra != rb:
        ra = ra if isinstance(ra, dict) else {}
        rb = rb if isinstance(rb, dict) else {}
        for key in REPEAT_FIELDS:
            if ra.get(key) != rb.get(key):
                lines.append("repeat_contact.%s: %s -> %s"
                             % (key, _fmt(ra.get(key)), _fmt(rb.get(key))))
        if ra.get("note") != rb.get("note"):
            lines.append("repeat_contact.note: changed")
    if old.get("version") != new.get("version"):
        lines.append("version: %s -> %s" % (_fmt(old.get("version")), _fmt(new.get("version"))))
    known = {"public_reply_rules", "escalate_always", "repeat_contact", "version"}
    other = sorted(k for k in set(old) | set(new)
                   if k not in known and old.get(k) != new.get(k))
    if other:
        lines.append("other top-level keys changed: %s" % ", ".join(other))
    return lines


# ---------------------------------------------------------------- brand conformance

def brand_line(repo, proposed_rules_path) -> Optional[str]:
    if proposed_rules_path is None:
        return None
    brands = Path(repo) / "brands"
    if not (brands / BRAND / "guidelines.md").is_file():
        return "brand conformance: skipped (no %s)" % BRAND_FILE
    code, message = conformance(BRAND, proposed_rules_path, brands)
    if code == 1:
        keys = message.rsplit("keys=", 1)[-1]
        return ("warning: proposed rules differ from %s (keys=%s); brands/ is not changed by "
                "this proposal" % (BRAND_FILE, keys))
    if code == 0:
        return "brand conformance: ok"
    return "warning: brand conformance not checked: %s" % " ".join(
        message.replace("error: ", "", 1).split())


# ---------------------------------------------------------------- CLI

def default_confirm(prompt: str) -> bool:
    """Ask the owner; refuse outright when stdin is not a terminal."""
    if not sys.stdin.isatty():
        print("error: %s" % NOT_A_TERMINAL, file=sys.stderr)
        return False
    print(prompt)
    try:
        return input("Type yes to continue: ").strip() == "yes"
    except EOFError:
        return False


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m triage.propose_change",
                                description=__doc__.splitlines()[0])
    p.add_argument("--repo", required=True, help="the owner's git work tree")
    p.add_argument("--cases", required=True, help="synthetic cases JSON")
    p.add_argument("--rules", default=None, help="a proposed full rules file")
    p.add_argument("--threshold", default=None, help="a proposed confidence threshold")
    p.add_argument("--dry-run", action="store_true",
                   help="validate and preview only; no branch, commit, push or PR")
    return p


def _raw_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _one_line(e) -> str:
    return " ".join(str(e).split())


def main(argv=None, client: Optional[ChangeClient] = None, confirm=None, tiers=None) -> int:
    """The CLI. `client`, `confirm` and `tiers` are test seams."""
    args = _parser().parse_args(argv)
    try:
        return _main(args, client, confirm, tiers)
    except (ChangeError, CasesError, RulesError, OSError, ValueError) as e:
        print("error: %s" % _one_line(e), file=sys.stderr)
        return 2


def _main(args, client, confirm, tiers) -> int:
    if args.rules is None and args.threshold is None:
        raise ChangeError("nothing proposed: give --rules or --threshold")
    repo = Path(args.repo)
    now = datetime.now(timezone.utc)
    run_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    branch = BRANCH_PREFIX + now.strftime("%Y%m%dT%H%M%SZ")

    cases = load_cases(args.cases)
    try:
        current = load_effective(repo / RULES_PATH)
    except RulesError as e:
        raise ChangeError("current rules do not load: %s" % _one_line(e))
    old_threshold, old_file_value = current_threshold(repo)
    proposed, new_threshold = current, old_threshold
    if args.rules is not None:
        try:
            proposed = load_effective(args.rules)
        except RulesError as e:
            raise ChangeError("proposed rules refused: %s" % _one_line(e))
        if proposed.repeat_contact is None and any("customer" in c for c in cases):
            raise ChangeError("proposed rules refused: repeat_contact is missing but the "
                              "cases have customers")
    if args.threshold is not None:
        new_threshold = parse_threshold(args.threshold)

    old_data = _raw_json(repo / RULES_PATH)
    new_data = _raw_json(args.rules) if args.rules is not None else old_data
    written = []
    if new_data != old_data:
        written.append(RULES_PATH)
    threshold_change = None
    if args.threshold is not None and new_threshold != old_file_value:
        written.append(THRESHOLDS_PATH)
        threshold_change = ((old_file_value, old_threshold), new_threshold)
    if not written:
        raise ChangeError("no change: the proposal equals the current files")

    with open(args.cases, "rb") as f:
        revision = "sha256:" + hashlib.sha256(f.read()).hexdigest()
    before = preview(cases, current, old_threshold, tiers, run_at=run_at,
                     gold_revision=revision)
    after = preview(cases, proposed, new_threshold, tiers, run_at=run_at,
                    gold_revision=revision)
    table = preview_table(before, after)
    if old_file_value is None:
        print("current threshold: code default %s (thresholds file value is null)"
              % old_threshold)
    else:
        print("current threshold: %s (from %s)" % (old_threshold, THRESHOLDS_PATH))
    if args.threshold is not None:
        print("proposed threshold: %s (recorded experiment, not operational)" % new_threshold)
    changes = what_changed(old_data, new_data, threshold_change)
    print("What changed:")
    for line in changes:
        print("- %s" % line)
    print(table)
    brand = brand_line(repo, args.rules)
    if brand is not None:
        print(brand)
    if args.dry_run:
        print("dry run: no branch, commit, push or PR")
        return 0

    if THRESHOLDS_PATH in written and \
            not isinstance((_read_thresholds(repo) or {}).get("confidence_threshold"), dict):
        raise ChangeError("cannot record the threshold: %s has no confidence_threshold object"
                          % THRESHOLDS_PATH)
    prompt = "Will create branch %s (draft PR into main) changing: %s" % (
        branch, ", ".join(written))
    if not (confirm or default_confirm)(prompt):
        raise ChangeError("not confirmed; nothing was changed")

    body = _pr_body(changes, table, brand, threshold_change is not None)
    client = client if client is not None else GitGhClient(repo)
    url = _change(client, branch, written, args.rules, new_threshold, body)
    print("draft PR opened: %s (branch %s)" % (url, branch))
    return 0


def _pr_body(changes, table, brand, threshold_changed) -> str:
    parts = ["## What changed", ""] + ["- %s" % c for c in changes] + ["", table, ""]
    if brand is not None:
        parts += [brand, ""]
    if threshold_changed:
        parts += [THRESHOLD_NOTE, ""]
    parts.append("Opened as a draft. Only the owner merges it.")
    return "\n".join(parts) + "\n"


def _change(client, branch, written, rules_path, threshold, body) -> str:
    base_sha = client.capture_base()
    worktree = None
    try:
        worktree = Path(client.add_worktree(branch, base_sha))
        if RULES_PATH in written:
            target = worktree / RULES_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(Path(rules_path).read_bytes())
        if THRESHOLDS_PATH in written:
            write_threshold(worktree / THRESHOLDS_PATH, threshold)
        client.commit(list(written), "Propose policy change (%s)" % ", ".join(written))
        client.verify(branch, base_sha, list(written))
        client.push(branch)
        return client.create_draft_pr(branch, "main", "Policy change proposal %s"
                                      % branch[len(BRANCH_PREFIX):], body)
    finally:
        if worktree is not None:
            try:
                client.remove_worktree()
            except Exception as e:  # report and keep the original outcome
                print("temporary worktree not removed: %s (%s)" % (worktree, _one_line(e)),
                      file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
