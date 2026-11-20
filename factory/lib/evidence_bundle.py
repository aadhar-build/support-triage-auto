"""Export selected local factory observations and finite-schema evidence receipts."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import html
import json
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import factory as F
from evidence_adapter import _bindings, _required
from observations import (_check_directory_entry, _open_directory, _project, _read_records,
                          decode_event_json)

KINDS = {"check_evidence": "check", "criterion": "criterion", "demo": "demo"}
SLOT = re.compile(r"evidence/check-([1-9][0-9]{0,2})\.json")
GATE_SELECTION = re.compile(r"round-[1-9][0-9]{0,5}/checks\.md@[0-9a-f]{64}")
ARTIFACT_SELECTION = re.compile(
    r"((?:round-([1-9][0-9]{0,5})/(?:checks|review-(?:code|security|eval))|final-checks|decision)\.md)"
    r"@([0-9a-f]{64})"
)
GATE_HEADER = re.compile(
    rb"GATE: (?P<verdict>PASS|FAIL)\n"
    rb"(?:CANDIDATE: (?P<candidate_sha>[0-9a-f]{40}|[0-9a-f]{64})\n"
    rb"BASE: (?P<base_sha>[0-9a-f]{40}|[0-9a-f]{64})\n"
    rb"SPEC-DIGEST: (?P<spec_digest>[0-9a-f]{64})\n)?\n## Summary\n"
)
NATIVE_KINDS = {
    "checks.md": "gate",
    "review-code.md": "review",
    "review-security.md": "review",
    "review-eval.md": "review",
    "decision.md": "decision",
    "final-checks.md": "final_gate",
}
DECISION_HEADERS = {b"APPROVE": "approve", b"ESCALATE": "escalate"}
NATIVE_REQUIREMENTS = {
    "checks": ("Round gate", "gate"),
    "final_checks": ("Final gate", "final_gate"),
    "review:code": ("Code review", "review"),
    "review:security": ("Security review", "review"),
    "review:eval": ("Eval review", "review"),
    "approval": ("Technical decision", "decision"),
}
MAX_SELECTED = 32
MAX_ROWS = 256


class BundleError(ValueError):
    pass


def _canonical(kind, verdict):
    fields = {"kind": kind, "schema_version": 1, "verdict": verdict}
    return (json.dumps(fields, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


# A finite set of entire permitted byte strings. No user-supplied string is
# passed through, even in an unknown JSON field, duplicate key, or whitespace.
PERMITTED = {(kind, verdict): _canonical(kind, verdict) for kind in KINDS.values()
             for verdict in ("pass", "fail", "pending")}


def _artifact_rows(artifacts, job_round):
    seen = set()
    for selection in artifacts:
        path, number, digest = selection.groups()
        if number is not None and int(number) != job_round:
            raise BundleError("Select round artifacts for the current job round.")
        if path in seen:
            raise BundleError("Select each native artifact once.")
        seen.add(path)
        yield ({"kind": NATIVE_KINDS[path.rsplit("/", 1)[-1]], "reference": path,
                "reported_verdict": "unknown", "claim_verification": "unverified",
                "candidate_binding": "unverified"}, digest)


@contextmanager
def _directory(path, forbidden=()):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise BundleError("Choose a direct local directory path.")
    with ExitStack() as stack:
        fd = stack.enter_context(_open_directory(path.anchor))
        for component in path.parts[1:]:
            fd = stack.enter_context(_open_directory(component, fd))
            if any(os.path.samestat(os.fstat(fd), item) for item in forbidden):
                raise BundleError("Choose a destination outside factory job state and Git metadata.")
        yield fd


def _read_file(directory, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise BundleError("File is outside the export policy.")
        content = stream.read(limit + 1)
        after = os.fstat(fd)
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (before.st_ctime_ns != after.st_ctime_ns or len(content) != before.st_size
                or after.st_nlink != 1 or not os.path.samestat(after, entry)):
            raise BundleError("File changed while reading.")
    return content


def _snapshot(root, job_fd, job_id, cutoff, overview):
    raw = _read_file(job_fd, "job.json", 64 * 1024)
    job = F.validate_reporting_job(decode_event_json(raw), job_id)
    events = {}
    try:
        observations = _open_directory("observations", job_fd)
        with observations as directory:
            fcntl.flock(directory, fcntl.LOCK_SH | fcntl.LOCK_NB)
            records, errors = _read_records(directory, job_id, for_transition=True)
            events, _, remaining = _project(records, cutoff)
            _check_directory_entry(job_fd, directory)
            if errors or remaining:
                raise BundleError("Observation coverage is incomplete. Review the local ledger.")
    except FileNotFoundError:
        # A missing ledger supplies no eligible evidence, including on a re-read.
        events = {}
    config = F.load_config(root)
    spec, spec_status = _read_spec(job_fd)
    bindings, _ = _bindings(root, config, job, spec)
    requirements = _overview_requirements(config, job_fd, job, cutoff, spec, spec_status) if overview else None
    return raw, job, events, bindings, requirements


def _read_spec(job_fd):
    try:
        spec = _read_file(job_fd, "spec.md", 256 * 1024)
        spec.decode("utf-8")
        return spec, "missing"
    except FileNotFoundError:
        return None, "missing"
    except (BundleError, OSError, UnicodeError):
        return None, "withheld"


@contextmanager
def _source_parent(job_fd, path):
    parent, _, name = path.rpartition("/")
    with ExitStack() as stack:
        directory = stack.enter_context(_open_directory(parent, job_fd)) if parent else job_fd
        yield directory, name


def _source_presence(job_fd, path):
    if path:
        try:
            with _source_parent(job_fd, path) as (directory, name):
                os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "withheld"
        return "withheld"
    return "missing"


def _overview_requirements(config, job_fd, job, cutoff, spec, spec_status):
    requirements, criteria = [], {}
    additional_review = 0
    for item in _required(config, job, spec, job.get("round") or None, cutoff):
        identifier = item["id"]
        if identifier.startswith("criterion:"):
            criteria[identifier.removeprefix("criterion:")] = f"Criterion {len(criteria) + 1}"
        elif identifier in NATIVE_REQUIREMENTS:
            label, kind = NATIVE_REQUIREMENTS[identifier]
            path = next(iter(item["legacy_paths"]), None)
            requirements.append((label, kind, item.get("required", True), path, _source_presence(job_fd, path)))
        elif identifier.startswith("review:"):
            additional_review += 1
            path = next(iter(item["legacy_paths"]), None)
            requirements.append((f"Additional review {additional_review}", "review", item["required"],
                                 None, _source_presence(job_fd, path)))
    if not criteria:
        requirements.append(("Acceptance criteria unavailable", "criterion", True, None, spec_status))
    return requirements, criteria


def _overview_rows(rows, requirements, criteria):
    native = {row["reference"]: row for row, _ in rows if row["kind"] in NATIVE_KINDS.values()}
    for label, kind, required, path, status in requirements:
        if path in native:
            native[path].update(label=label, required=required)
        else:
            row = _missing_row(label, kind, required, status)
            if path:
                row["reference"] = path
            rows.append((row, None))
    present = {row.get("label") for row, _ in rows}
    rows.extend((_missing_row(label, "criterion", True), None) for label in criteria.values() if label not in present)
    if "Working-software demo" not in present:
        rows.append((_missing_row("Working-software demo", "demo", None), None))


def _missing_row(label, kind, required, status="missing"):
    row = {"label": label, "kind": kind, "reported_verdict": "unknown",
           "claim_verification": "unverified", "candidate_binding": "unverified", "artifact_status": status}
    if required is not None:
        row["required"] = required
    return row


def _binding_status(bindings, records):
    missing = False
    for key, expected in bindings.items():
        for record in records:
            value = record.get(key)
            if value is None:
                continue
            if expected is None:
                missing = True
            elif value != expected:
                return "stale"
    return "withheld" if missing else None


def _rows(job, events, bindings, selected, criteria):
    rows = []
    targets = set()
    origin = "controlled" if job.get("origin") == "controlled" else "real"
    for event in events.values():
        kind = KINDS.get(event["kind"])
        if not kind or event["origin"] != origin:
            continue
        data = event["data"]
        verdict = data.get("verdict") or "unknown"
        evidence = event.get("evidence") or []
        binding = _binding_status(bindings, (event, data, *evidence))
        label = None
        if criteria is not None:
            if kind == "criterion":
                label = criteria.get(data.get("criterion_id"))
            elif kind == "check":
                check_id = data["check_id"]
                if check_id.startswith("criterion:") and binding is None:
                    label = criteria.get(check_id.removeprefix("criterion:"))
        for ref in evidence or [{}]:
            path = ref.get("path", "")
            slot = SLOT.fullmatch(path)
            listed = bool(slot and path in selected)
            row = {"kind": kind, "reported_verdict": verdict, "claim_verification": "unverified",
                   "candidate_binding": "stale" if binding == "stale" else "unverified",
                   "artifact_status": binding or ("withheld" if ref else "missing")}
            if criteria is not None:
                if label:
                    row.update(label=label, required=True)
                elif kind == "demo":
                    row["label"] = "Working-software demo"
            if slot:
                row["reference"] = path
            if listed:
                targets.add(path)
            rows.append((row, ref.get("sha256") if listed and binding is None else None))
            if len(rows) > MAX_ROWS:
                raise BundleError("Too many evidence rows for a local bundle.")
    if targets != selected:
        raise BundleError("A selected receipt is absent from the current job observations.")
    return rows


def _review_verdict(content):
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError:
        return None
    verdicts = set()
    in_header = False
    for line in lines:
        if not line.strip() and not in_header:
            continue
        header = F.HEADER_RE.match(line)
        if header is None:
            break
        in_header = True
        if header[1] == "VERDICT":
            verdicts.add(header[2].upper())
    if verdicts == {"PASS"}:
        return "pass"
    if verdicts == {"CHANGES"}:
        return "changes"
    return None


def _source(job_fd, row, expected, bindings):
    kind = row["kind"]
    native = kind in NATIVE_KINDS.values()
    if native:
        row["reported_verdict"] = "unknown"
    try:
        with _source_parent(job_fd, row["reference"]) as (directory, name):
            content = _read_file(directory, name, 256 * 1024 if native else 1024)
    except FileNotFoundError:
        return "missing", None
    except (BundleError, OSError):
        return "withheld", None
    if hashlib.sha256(content).hexdigest() != expected:
        return "stale", None
    if kind in ("gate", "final_gate"):
        header = GATE_HEADER.match(content)
        if not header:
            return "withheld", None
        recorded = {key: header[key].decode("ascii") for key in ("candidate_sha", "base_sha", "spec_digest")
                    if header[key] is not None}
        binding = _binding_status(bindings, (recorded,))
        if binding:
            if binding == "stale":
                row["candidate_binding"] = "stale"
            return binding, None
        verdict = header["verdict"].decode("ascii").lower()
        row["reported_verdict"] = verdict
        return "projected", PERMITTED["check", verdict]
    if kind == "review" or kind == "decision":
        verdict = (_review_verdict(content) if kind == "review"
                   else DECISION_HEADERS.get(content.partition(b"\n")[0].removesuffix(b"\r")))
        if verdict is None:
            return "withheld", None
        row["reported_verdict"] = verdict
        return "projected", _canonical(kind, verdict)
    if content != PERMITTED.get((kind, row["reported_verdict"])):
        return "withheld", None
    return "copied", content


def _write_file(directory, name, content):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        return os.fstat(fd)


_STYLE = """
* { box-sizing: border-box; }
body { margin: 0; color: #202527; background: #fff; font: 1rem/1.55 Inter, system-ui, sans-serif; }
main { max-width: 64rem; margin: auto; padding: clamp(1rem, 4vw, 3rem); }
h1 { line-height: 1.15; }
p { max-width: 72ch; }
a { color: #185c75; overflow-wrap: anywhere; }
table { width: 100%; margin-block: 1.5rem; border-collapse: collapse; }
th, td { text-align: left; vertical-align: top; padding: .6rem; border-bottom: 1px solid #bbb; }
th { background: #eef2f3; }
@media (max-width: 44rem) {
  table, tbody, tr { display: block; }
  thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
  tr { border-top: 1px solid #999; padding: .7rem 0; }
  td { display: grid; grid-template-columns: minmax(0, 7.5rem) minmax(0, 1fr);
       gap: .5rem; padding: .18rem 0; border: 0; overflow-wrap: anywhere; }
  td::before { content: attr(data-label); font-weight: 600; }
}
"""


def _page(manifest):
    esc = html.escape
    style_digest = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode("ascii")
    policy = f"default-src 'none'; base-uri 'none'; form-action 'none'; object-src 'none'; style-src 'sha256-{style_digest}'"
    lines = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f'<meta http-equiv="Content-Security-Policy" content="{policy}">',
             f"<style>{_STYLE}</style>",
             "<title>Job evidence</title></head><body><main><h1>Job evidence</h1>",
             f"<p>Issue {esc(str(manifest['issue']))} · Origin {esc(manifest['origin'])}</p>",
             "<p>This export covers check, criterion and demo observations for one job and selected factory reports. "
             "The bundle includes links for explicitly selected receipts and verdict projections.</p>",
             "<p>Projected reports include only the recorded verdict. Technical decisions and owner acceptance are separate. "
             "Current-candidate verification and owner acceptance remain unverified.</p>"]
    if overview := manifest.get("overview"):
        stage = esc(overview["stage"])
        number = esc(str(overview["round"] if overview["round"] is not None else "unknown"))
        candidate = esc(overview["candidate_sha"] or "unknown")
        lines.append(f"<p>Stage {stage} · Round {number}<br>Local candidate <code>{candidate}</code></p>")
        lines.append("<p>Criterion numbers follow the order in the local specification when it is available.</p>")
    lines.append("<table><thead><tr><th>Observation</th><th>Recorded verdict</th><th>Candidate binding</th>"
                 "<th>Evidence</th><th>Local receipt</th></tr></thead><tbody>")
    for row in manifest["rows"]:
        link = esc(row.get("reference", "—"))
        if "file" in row:
            link = f'<a href="{esc(row["file"])}">{esc(row["reference"])}</a>'
        labels = {"decision": "Technical decision", "final_gate": "Final gate"}
        label = row.get("label") or labels.get(row["kind"], row["kind"].capitalize())
        if row.get("required") is False:
            label += " · Optional"
        cells = (("Observation", esc(label)), ("Recorded verdict", esc(row["reported_verdict"])),
                 ("Candidate binding", esc(row["candidate_binding"])), ("Evidence", esc(row["artifact_status"])),
                 ("Local receipt", link))
        lines.append("<tr>" + "".join(f'<td data-label="{label}">{value}</td>' for label, value in cells) + "</tr>")
    lines.append('</tbody></table><p><a href="manifest.json">Manifest and digests</a></p></main></body></html>')
    return "\n".join(lines).encode("utf-8")


def _unlink_owned(directory, name, original):
    try:
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if os.path.samestat(entry, original):
        os.unlink(name, dir_fd=directory)


def _create_bundle(root, job_path, job_fd, selection, artifacts, output, parent, overview):
    cutoff = datetime.now(timezone.utc)
    snapshot = _snapshot(root, job_fd, job_path.name, cutoff, overview)
    _, job, events, bindings, context = snapshot
    rows = _rows(job, events, bindings, selection, context[1] if overview else None)
    rows.extend(_artifact_rows(artifacts, job.get("round")))
    if overview:
        _overview_rows(rows, *context)
    if len(rows) > MAX_ROWS:
        raise BundleError("Too many evidence rows for a local bundle.")
    os.mkdir(output.name, mode=0o700, dir_fd=parent)
    with _open_directory(output.name, parent) as directory:
        owned = {}
        try:
            files = {}
            for row, expected in rows:
                if not expected:
                    continue
                status, data = _source(job_fd, row, expected, bindings)
                row["artifact_status"] = status
                if data is None:
                    continue
                if row["kind"] == "gate":
                    name = f"gate-round-{job['round']}.json"
                elif row["kind"] in NATIVE_KINDS.values():
                    name = Path(row["reference"]).with_suffix(".json").name
                else:
                    number = int(SLOT.fullmatch(row["reference"]).group(1))
                    name = f"receipt-{number:03d}.json"
                files[name] = data
                row.update(file=name, source_sha256=expected, sha256=hashlib.sha256(data).hexdigest())

            for name, data in files.items():
                owned[name] = _write_file(directory, name, data)
            for row, expected in rows:
                if "file" in row:
                    status, content = _source(job_fd, row, expected, bindings)
                    if content != files[row["file"]]:
                        row["artifact_status"] = status if content is None else "stale"
                        del row["file"], row["sha256"], row["source_sha256"]
            retained = {row["file"] for row, _ in rows if "file" in row}
            for name in set(files) - retained:
                _unlink_owned(directory, name, owned.pop(name))
                del files[name]
            issue = job.get("issue")
            manifest = {"schema_version": 1, "scope": "one job with check, criterion and demo observations and selected factory reports",
                        "issue": issue if type(issue) is int and 0 < issue < 10**10 else "unknown",
                        "origin": job.get("origin") if job.get("origin") in ("controlled", "real") else "unspecified",
                        "rows": sorted((row for row, _ in rows), key=lambda row: json.dumps(row, sort_keys=True))}
            if overview:
                stage = job.get("stage")
                manifest["overview"] = {"stage": stage if stage in F.TRANSITIONS else "unknown",
                                        "round": job.get("round"), "candidate_sha": bindings["candidate_sha"]}
            files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode("ascii")
            files["index.html"] = _page(manifest)
            for name in ("manifest.json", "index.html"):
                owned[name] = _write_file(directory, name, files[name])
            if set(os.listdir(directory)) != set(files):
                raise BundleError("Destination contains unexpected files.")
            for name, content in files.items():
                entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not os.path.samestat(entry, owned[name]) or _read_file(directory, name, len(content)) != content:
                    raise BundleError("Bundle changed before publication.")
            with _directory(output) as current:
                if not os.path.samestat(os.fstat(current), os.fstat(directory)):
                    raise BundleError("Destination changed during export.")
            with _directory(job_path) as current:
                if (not os.path.samestat(os.fstat(current), os.fstat(job_fd))
                        or snapshot != _snapshot(root, current, job_path.name, cutoff, overview)):
                    raise BundleError("Job evidence changed during export.")
                for row, expected in rows:
                    if name := row.get("file"):
                        _, content = _source(current, row.copy(), expected, bindings)
                        if content != files[name]:
                            raise BundleError("Selected evidence changed before publication.")
        except BaseException:
            for name, original in owned.items():
                _unlink_owned(directory, name, original)
            entry = os.stat(output.name, dir_fd=parent, follow_symlinks=False)
            if os.path.samestat(entry, os.fstat(directory)) and not os.listdir(directory):
                os.rmdir(output.name, dir_fd=parent)
            raise


def export_job(root, job_id, output, selected, gate=None, *, artifacts=(), overview=False):
    root, output = Path(root).absolute(), Path(output).absolute()
    if gate is not None:
        if not GATE_SELECTION.fullmatch(gate):
            raise BundleError("Select a current round gate by path and expected SHA-256.")
        artifacts = (*artifacts, gate)
    selections = [ARTIFACT_SELECTION.fullmatch(artifact) for artifact in artifacts]
    if not all(selections):
        raise BundleError("Select a supported native artifact by path and expected SHA-256.")
    selection_count = len(selected) + len(selections)
    if (not F.valid_reporting_job_id(job_id) or selection_count > MAX_SELECTED or not (overview or selection_count)
            or not all(SLOT.fullmatch(path) for path in selected)):
        raise BundleError("Select a job and one to 32 allowed artifact or receipt paths.")
    if (not output.name or ".." in output.parts or any(part.casefold() == ".git" for part in output.parts)
            or output.is_relative_to(root / "factory" / "jobs")):
        raise BundleError("Choose a new directory outside factory job state and Git metadata.")
    job_path = root / "factory" / "jobs" / job_id
    try:
        with ExitStack() as stack:
            job_fd = stack.enter_context(_directory(job_path))
            forbidden = [os.stat("..", dir_fd=job_fd)]
            git_dir = root / ".git"
            if git_dir.is_dir():
                forbidden.append(git_dir.stat())
            parent = stack.enter_context(_directory(output.parent, forbidden))
            parent_stat = os.fstat(parent)
            if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o022:
                raise BundleError("Choose an owner-controlled destination parent.")
            _create_bundle(root, job_path, job_fd, set(selected), selections, output, parent, overview)
    except (OSError, F.FactoryError, ValueError) as error:
        if isinstance(error, BundleError):
            raise
        raise BundleError("Cannot export. Check source integrity and choose an unused local destination.") from error
