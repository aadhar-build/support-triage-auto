"""On-demand weekly reporting from read-only, repository-scoped job snapshots."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as tz
from decimal import Decimal, localcontext
from statistics import median
from zoneinfo import ZoneInfo

from evidence_adapter import evidence_reference, tracker_url
from factory import SMALL_SAMPLE, markdown_text, valid_reporting_job_id
from factory_recovery import recovery_summary


ACTIVITIES = ("preparation", "clarification", "supervision", "review", "recovery", "correction")
TERMINAL = {"merged", "failed", "rejected", "abandoned"}
COST_TYPES = ("subscription_usage", "subscription_charge", "incremental_charge",
              "api_equivalent_estimate")
BASELINE = ("Historical v1 baseline, frozen 18 Nov 2026. Four support-triage jobs merged. "
            "Owner minutes and cost were unmeasured. The owner-reported “1 day” has unresolved "
            "unit and scope.")


def instant(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime) and value.utcoffset() is not None:
        return value.astimezone(tz.utc)
    return None


def stamp(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def rate(numerator, eligible, covered):
    """A complete rate requires an outcome for every eligible member."""
    return dict(numerator=numerator, eligible=eligible, covered=covered, unknown=eligible-covered,
                percent=round(100 * numerator / eligible, 2) if eligible and covered == eligible else None)


def sample(values, eligible):
    return dict(median=round(median(values), 2) if values else None,
                sample=len(values), excluded=eligible-len(values))


def duration(start, end, unit):
    if start and end and end >= start:
        return round((end-start).total_seconds() / unit, 2)
    return None


def current_events(events, as_of):
    visible = {}
    for e in events:
        recorded = instant(e.get("recorded_at"))
        if e.get("origin") == "real" and recorded and recorded <= as_of:
            visible[e["event_id"]] = e
    superseded = {e.get("supersedes_event_id") for e in visible.values()}
    return sorted((e for key, e in visible.items() if key not in superseded
                   and (instant(e.get("occurred_at")) or as_of) <= as_of),
                  key=lambda e: (instant(e.get("occurred_at")) or instant(e["recorded_at"]),
                                 instant(e["recorded_at"]), e["event_id"]))


def covers(intervals, start, end):
    """Coverage can span adjacent complete declarations, without a gap."""
    if start is None or end is None or end < start:
        return False
    cursor = start
    for left, right in sorted((a, b) for a, b in intervals if a and b and a <= b):
        if left <= cursor <= right:
            cursor = right
            if cursor >= end:
                return True
    return False


@dataclass
class Period:
    start: datetime
    end: datetime
    as_of: datetime

    def contains(self, when):
        return when is not None and self.start <= when < self.end and when <= self.as_of


class Job:
    def __init__(self, record, as_of):
        self.record = record
        self.raw = record["job"]
        self.id = self.raw["id"]
        self.url = tracker_url(record.get("url"))
        self.events = current_events(record.get("events", []), as_of)
        self.diagnostics = list(record.get("diagnostics", []))
        self.by_kind = defaultdict(list)
        for event in self.events:
            self.by_kind[event["kind"]].append(event)
        self.context = {}
        for event in self.by_kind["task_context"]:
            self.context.update({k: v for k, v in event["data"].items() if v is not None})
        history = [(instant(row.get("at")), row.get("stage")) for row in self.raw.get("history", [])]
        self.history = sorted(((when, stage) for when, stage in history if when and when <= as_of),
                              key=lambda item: item[0])
        submission_notes = {(instant(row.get("at")), row.get("stage")) for row in self.raw.get("history", [])
                            if str(row.get("note", "")).startswith("auto-merge refused:")
                            or row.get("note") == "merge requires human verification of the PR head and base"}
        self.future = bool(history and all(when and when > as_of for when, _ in history))
        self.status = self.history[-1][1] if self.history else self.raw.get("stage")
        boundary = self._boundary("ended_at", as_of)
        context_status = self.context.get("status")
        if context_status and (context_status not in TERMINAL or boundary):
            # Explicit lifecycle observations also cover jobs outside the stage machine.
            when = boundary or instant(self.by_kind["task_context"][-1].get("occurred_at"))
            if not self.history or (when and when >= self.history[-1][0]):
                self.status = context_status
        self.created = self.first("intake") or self._boundary("started_at", as_of)
        self.started = self._boundary("started_at", as_of) or self.created
        self.merged = self.first("merged") or (boundary if self.status == "merged" else None)
        authoritative_merge = instant(record.get("merged_at"))
        if authoritative_merge is not None:
            self.merged = authoritative_merge if authoritative_merge <= as_of else None
            if self.merged:
                self.status = "merged"
                self.future = False
            elif self.status == "merged":
                self.status = next((s for _, s in reversed(self.history) if s != "merged"), None)
        self.ended = self.merged or self.first(*(TERMINAL - {"merged"}))
        if self.ended is None and authoritative_merge is None:
            self.ended = boundary
        self.submitted = self._boundary("submitted_at", as_of)
        self.ready = None
        for i, (when, stage) in enumerate(self.history):
            previous = self.history[i-1][1] if i else None
            if stage == "needs-human" and (previous == "approve" or
                                           (previous == "merging" and (when, stage) in submission_notes)):
                self.submitted = self.submitted or when
                self.ready = when
                break
            if stage == "merged" and previous == "merging":
                self.ready = when
                break
        self.ready = self.ready or self.submitted
        self.guard = record.get("guardrails") or {}
        self.guard_diagnostics = self.diagnostics + list(self.guard.get("diagnostics", []))

    def first(self, *stages):
        return next((when for when, stage in self.history if stage in stages), None)

    def _boundary(self, name, as_of):
        point = instant(self.context.get(name))
        return point if point and point <= as_of else None

    def during(self, kind, period):
        return [e for e in self.by_kind[kind] if period.contains(instant(e.get("occurred_at")))]

    def active(self, p):
        if self.future or p.as_of < p.start:
            return False
        if self.started and self.started < p.end and (self.ended is None or self.ended >= p.start):
            return True
        return (p.contains(self.merged) or any(p.contains(t) for t, _ in self.history)
                or any(p.contains(instant(e.get("occurred_at"))) for e in self.events
                       if e["kind"] not in {"observation_coverage", "owner_effort_coverage", "usage_cost"}))

    def covered(self, kind, scopes, start, end, *, candidate_sha=None):
        if self.diagnostics or not start or not end or end < start:
            return False
        if (kind == "owner_effort_coverage" and self.raw.get("owner_minutes") is not None
                and not self.by_kind["owner_effort"]):
            return False
        intervals = []
        for e in self.by_kind[kind]:
            if candidate_sha and e.get("candidate_sha") != candidate_sha:
                continue
            data = e["data"]
            if data.get("scope") not in scopes or data.get("complete") is not True:
                continue
            observed, recorded = instant(e.get("occurred_at")), instant(e.get("recorded_at"))
            if observed is None or recorded is None:
                continue
            horizon = min(observed, recorded)
            left, right = data.get("window_start"), data.get("window_end")
            if kind == "owner_effort_coverage" and left is None and right is None:
                intervals.append((start, horizon))
            elif instant(left) and instant(right):
                intervals.append((instant(left), min(instant(right), horizon)))
        return covers(intervals, start, end)

    def full_effort(self):
        bounds = [instant(e.get("occurred_at")) for e in self.by_kind["owner_effort"]]
        if self.started and self.ended and all(bounds):
            return self.covered("owner_effort_coverage", {"job", "owner_effort"},
                                min([self.started, *bounds]), max([self.ended, *bounds]))
        return False


def effort_report(jobs, merged, period):
    minutes = defaultdict(list)
    rework = []
    unknown_rework = unclassified_minutes = missing_rework_amounts = 0
    known = []
    covered = 0
    rework_flags = rework_covered = 0
    for job in jobs:
        begin = max(period.start, job.started or period.start)
        end = min(period.end, period.as_of, job.ended or period.as_of)
        rows = job.during("owner_effort", period)
        # Preparation can precede intake, and correction can follow delivery.
        if rows:
            effort_times = [instant(e["occurred_at"]) for e in rows]
            begin = min(begin, min(effort_times))
            end = max(end, max(effort_times))
        complete = job.covered("owner_effort_coverage", {"job", "owner_effort"}, begin, end)
        undated = any(not instant(e.get("occurred_at")) for e in job.by_kind["owner_effort"])
        complete = complete and not undated and all(e["data"].get("minutes") is not None for e in rows)
        covered += complete
        rework_covered += complete and all(row["data"].get("rework") is not None for row in rows)
        for e in rows:
            d = e["data"]
            if d.get("rework") is None:
                unknown_rework += 1
                unclassified_minutes += d.get("minutes") or 0
            if d.get("minutes") is None and d.get("rework") is not False:
                missing_rework_amounts += 1
            if d.get("minutes") is not None:
                known.append(d["minutes"])
                minutes[d.get("activity")].append(d["minutes"])
                if d.get("rework") is True:
                    rework.append(d["minutes"])
                if d.get("rework") is not None:
                    rework_flags += 1
        if complete:
            known.append(0)
    full = {}
    for job in jobs:
        values = [e["data"].get("minutes") for e in job.by_kind["owner_effort"]]
        if job.full_effort() and all(n is not None for n in values):
            full[job.id] = sum(values)
    stats = sample([full[j.id] for j in merged if j.id in full], len(merged))
    all_covered = len(jobs) == covered
    return dict(known_minutes=sum(known) if known or not jobs else None, covered_jobs=covered,
                eligible_jobs=len(jobs), unknown_jobs=len(jobs)-covered,
                activities={a: sum(minutes[a]) if minutes[a] or all_covered else None for a in ACTIVITIES},
                rework_minutes=sum(rework) if rework_flags or rework_covered or (all_covered and not unknown_rework) else None,
                rework_covered_jobs=rework_covered,
                rework_total_minutes=sum(rework) if all_covered and not (unknown_rework or missing_rework_amounts) else None,
                unknown_rework_entries=unknown_rework, unclassified_minutes=unclassified_minutes,
                unknown_rework_amount_entries=missing_rework_amounts,
                per_merge_median=dict(minutes=stats["median"], sample=stats["sample"],
                                      excluded=stats["excluded"])), full


def delivery(merged, active, p):
    leads = [duration(j.created, j.merged, 3600) for j in merged]
    times = [duration(j.started, j.ready, 60) for j in merged]
    attempted = covered = passed = 0
    for job in active:
        gate = job.record.get("first_gate") or {}
        when = instant(gate.get("attempted_at")) or job.first("gate")
        if not p.contains(when):
            continue
        attempted += 1
        if gate.get("verified") is True and gate.get("status") in {"pass", "fail"}:
            covered += 1
            passed += gate["status"] == "pass"
    return dict(count=len(merged), jobs=[j.id for j in merged],
                merge_evidence={j.id: j.record["merge_evidence"] for j in merged if j.record.get("merge_evidence")},
                lead_hours=sample([v for v in leads if v is not None], len(merged)),
                agent_elapsed_minutes=sample([v for v in times if v is not None], len(merged)),
                first_gate=rate(passed, attempted, covered))


def outcomes(active, p):
    counts = Counter()
    states = Counter()
    eligible = known = 0
    for j in active:
        decisions = [e for e in j.by_kind["owner_decision"] if e["source"]["kind"] == "owner"
                     and instant(e.get("occurred_at")) and (not j.submitted or instant(e["occurred_at"]) >= j.submitted)]
        # An explicit owner verdict concerns the reviewed revision. Direct, non-code
        # tasks can have a job-level owner decision with no candidate bindings.
        candidate = j.guard.get("candidate") or {}
        if j.merged:
            expected_sha = j.record.get("merged_candidate_sha")
            expected_round = j.record.get("merged_round")
        else:
            expected_sha = j.record.get("submitted_candidate_sha") or candidate.get("candidate_sha")
            expected_round = j.record.get("submitted_round") or candidate.get("round") or j.raw.get("round")
        direct = j.context.get("workflow_mode") == "direct"
        decisions = [e for e in decisions
                     if ((expected_sha and e.get("candidate_sha") == expected_sha and expected_round == e.get("round"))
                         or (direct and not expected_sha and e.get("candidate_sha") is None and e.get("round") is None))]
        decision = decisions[-1]["data"] if decisions else {}
        state = decision.get("disposition") or j.status or "unknown"
        states[state] += 1
        if not j.submitted:
            if j.status in {"failed", "abandoned", "rejected"} and j.ended:
                counts["no_recorded_submission"] += 1
            else:
                counts["submission_unobserved"] += 1
        if not p.contains(j.submitted):
            continue
        eligible += 1
        disposition = decision.get("disposition")
        if disposition == "accepted" and isinstance(decision.get("material_follow_up"), bool):
            counts["accepted_with_follow_up" if decision["material_follow_up"] else "accepted_as_built"] += 1
            known += 1
        elif disposition in {"rejected", "abandoned"} or j.status in {"rejected", "abandoned", "failed"}:
            counts[disposition if disposition in {"rejected", "abandoned"} else j.status] += 1
            known += 1
        else:
            counts["unobserved" if disposition or j.status in TERMINAL else "pending"] += 1
    breakdown = {k: counts[k] for k in ("accepted_as_built", "accepted_with_follow_up", "rejected",
                                         "abandoned", "failed", "pending", "unobserved",
                                         "no_recorded_submission", "submission_unobserved")}
    breakdown["rate"] = rate(counts["accepted_as_built"], eligible, known)
    return breakdown, dict(states)


def defects(merged, days, as_of):
    result = dict(window_days=days, rate=None, immature=None, known_affected_jobs=[],
                  unwindowed_observations=sum(len(j.by_kind["defect"]) for j in merged))
    if days is None:
        result["reason"] = "Escaped-defect observation window has not been set."
        return result
    mature = known = affected = 0
    for j in merged:
        end = j.merged + timedelta(days=days)
        if end > as_of:
            continue
        mature += 1
        sha = j.record.get("merged_candidate_sha")
        positive = uncertain = False
        for e in j.by_kind["defect"]:
            d = e["data"]
            when = instant(d.get("discovered_at"))
            if when and not j.merged <= when < end:
                continue
            if when and d.get("origin") == "escaped" and sha and sha == e.get("candidate_sha"):
                positive = True
            elif not sha or not e.get("candidate_sha") or sha == e["candidate_sha"]:
                uncertain = True
        if positive:
            affected += 1
            result["known_affected_jobs"].append(j.id)
        if positive or (sha and not uncertain and j.covered(
                "observation_coverage", {"escaped_defects"}, j.merged, end, candidate_sha=sha)):
            known += 1
    result.update(rate=rate(affected, mature, known), immature=len(merged)-mature)
    return result


@dataclass(eq=False)
class CostItem:
    dimension: tuple
    amount: Decimal | None
    status: str
    ambiguous_issuer: bool


def exact_amount(values):
    if not values:
        return None
    # Retain every input decimal place, plus enough integer digits for the sum.
    integer_digits = max(1, max(v.adjusted() + 1 for v in values))
    fraction_digits = max(0, max(-v.as_tuple().exponent for v in values))
    with localcontext() as context:
        context.prec = integer_digits + fraction_digits + len(str(len(values)))
        return format(sum(values), "f")


def cost_ledger(jobs, totals):
    """Resolve one source identity across every job and currency before assigning amounts."""
    in_week = {(item["job"], item["event_id"]) for items in totals.values() for item in items}
    identities = defaultdict(list)
    for job in jobs:
        for event in job.by_kind["usage_cost"]:
            data, source = event["data"], event["source"]
            if data.get("billing_item"):
                identity = ("billing", data["billing_item"])
            else:
                identity = ("window", source["kind"], source["record_id"],
                            data.get("window_start"), data.get("window_end"))
            identities[identity].append((job.id, event))

    weekly, by_job = defaultdict(list), defaultdict(list)
    for entries in identities.values():
        issuers = defaultdict(list)
        for job_id, event in entries:
            issuers[event["source"].get("provider")].append((job_id, event))
        ambiguous = len(entries) > 1 and None in issuers
        # An unscoped reference can refer to any issuer with the same item ID.
        for records in ([entries] if ambiguous else issuers.values()):
            dimensions, amounts = set(), set()
            observed_jobs = defaultdict(set)
            complete = True
            for job_id, event in records:
                data = event["data"]
                unit = (data["cost_type"], data.get("unit"), data.get("currency"))
                dimensions.add(unit)
                amounts.add(Decimal(str(data["amount"])) if data.get("amount") is not None else None)
                if (job_id, event["event_id"]) in in_week:
                    observed_jobs[unit].add(job_id)
                else:
                    complete = False
            for unit, owners in observed_jobs.items():
                if ambiguous or not complete or len(dimensions) != 1 or len(amounts) != 1:
                    item = CostItem(unit, None, "conflicting", ambiguous)
                else:
                    item = CostItem(unit, next(iter(amounts)), "shared" if len(owners) > 1 else "allocated", False)
                weekly[unit].append(item)
                for job_id in owners:
                    by_job[job_id].append(item)
    return weekly, by_job


def cohort_costs(jobs, allocations):
    items = defaultdict(set)
    observed = defaultdict(set)
    for job in jobs:
        for item in allocations[job.id]:
            items[item.dimension].add(item)
            observed[item.dimension[0]].add(job.id)
    totals = []
    for (kind, unit, currency), entries in sorted(items.items(), key=lambda e: tuple(v or "" for v in e[0])):
        counts = Counter(item.status for item in entries)
        known = exact_amount([item.amount for item in entries if item.status == "allocated"])
        totals.append(dict(cost_type=kind, unit=unit, currency=currency, known_amount=known,
                           recorded_amount=known if counts["allocated"] == len(entries) else None,
                           allocated_items=counts["allocated"], shared_items=counts["shared"],
                           conflicting_items=counts["conflicting"]))
    coverage = {kind: dict(covered=len(observed[kind]), eligible=len(jobs), unknown=len(jobs)-len(observed[kind]))
                for kind in COST_TYPES}
    return totals, coverage


def costs(jobs, active, period):
    totals = defaultdict(list)
    unattributed = []
    for j in jobs:
        for e in j.by_kind["usage_cost"]:
            d = e["data"]
            left, right = instant(d.get("window_start")), instant(d.get("window_end"))
            if left or right:
                if (right and right <= period.start) or (left and left >= period.end):
                    continue
                if not (left and right and period.start <= left <= right <= min(period.end, period.as_of)):
                    unattributed.append(dict(job=j.id, event_id=e["event_id"], reason="partial billing window"))
                    continue
            elif not period.contains(instant(e.get("occurred_at"))):
                if not e.get("occurred_at"):
                    unattributed.append(dict(job=j.id, event_id=e["event_id"], reason="unknown occurrence"))
                continue
            if d.get("amount") is None:
                unattributed.append(dict(job=j.id, event_id=e["event_id"], reason="unknown amount"))
                continue
            key = (d["cost_type"], d.get("unit"), d.get("currency"))
            totals[key].append(dict(job=j.id, source=e.get("source"), event_id=e["event_id"],
                                    origin=e["origin"], amount=str(d["amount"]), billing_item=d.get("billing_item"),
                                    window_start=d.get("window_start"), window_end=d.get("window_end")))
    ledger, allocations = cost_ledger(jobs, totals)
    rows = []
    for (kind, unit, currency), items in totals.items():
        entries = ledger[kind, unit, currency]
        conflict = any(item.status == "conflicting" for item in entries)
        amount = None if conflict else exact_amount([item.amount for item in entries])
        rows.append(dict(cost_type=kind, unit=unit, currency=currency, amount=amount, conflict=conflict,
                         ambiguous_billing_item=any(item.ambiguous_issuer for item in entries), items=items))
    coverage = {}
    active_ids = {job.id for job in active}
    for kind in COST_TYPES:
        ids = {item["job"] for row in rows if row["cost_type"] == kind for item in row["items"]}
        coverage[kind] = dict(jobs_with_amount=len(ids & active_ids), eligible_jobs=len(active_ids),
                              other_jobs_with_amount=len(ids - active_ids))
    return dict(totals=rows, coverage=coverage, unattributed=unattributed), allocations


def escalations(jobs, p):
    counts = Counter()
    for job in jobs:
        requests = defaultdict(list)
        for e in job.by_kind["escalation"]:
            requests[e["data"]["request_id"]].append(e)
        for observations in requests.values():
            if not p.contains(instant(observations[0].get("occurred_at"))):
                continue
            judgments = {e["data"]["rating"] for e in observations
                         if e["source"]["kind"] == "owner" and e["data"].get("rating")}
            counts[next(iter(judgments)) if len(judgments) == 1 else "unrated"] += 1
    rated = counts["necessary"] + counts["unnecessary"]
    coverage = sum(j.covered("observation_coverage", {"escalations"}, p.start, min(p.end, p.as_of))
                   for j in jobs)
    return dict(precision=rate(counts["necessary"], rated, rated), rated_requests=rated,
                unrated=counts["unrated"], unnecessary=counts["unnecessary"], missed=counts["missed"],
                covered_jobs=coverage, eligible_jobs=len(jobs))


def guardrails(jobs, p):
    gaps = []
    issues = Counter()
    covered = eligible = proven = applicable_jobs = fully_covered = 0
    blocked = effective = suspected = incident_coverage = 0
    unknown_incidents = 0
    recovery = Counter()
    recovery_minutes = []
    for j in jobs:
        guard = j.guard
        c = guard.get("coverage") or {}
        if c.get("eligible", 0):
            applicable_jobs += 1
            eligible += c["eligible"]
            covered += c["covered"]
            proven += c["proven"]
            fully_covered += c["covered"] == c["eligible"] and not j.guard_diagnostics
        for row in guard.get("requirements", []):
            if row.get("required") and row["status"] not in {"pass", "not_required"}:
                gaps.append(dict(job=j.id, requirement=row["id"], status=row["status"],
                                 evidence=row.get("evidence") or [], legacy_paths=row.get("legacy_paths") or []))
                issues[(row["id"], row["status"])] += 1
        incidents = guard.get("incidents", {})
        intervals = [(instant(w.get("window_start")), instant(w.get("window_end")))
                     for w in incidents.get("coverage_windows", [])
                     if w.get("scope") == "guardrails" and w.get("complete") and w.get("verified")]
        if not j.guard_diagnostics and covers(intervals, p.start, min(p.end, p.as_of)):
            incident_coverage += 1
        for row in incidents.get("records", []):
            if row.get("scope") not in {"guardrails", None}:
                continue
            when = instant(row.get("occurred_at"))
            if when and not p.contains(when):
                continue
            verified = row.get("verified") is True and row.get("status") == "confirmed"
            if not when:
                unknown_incidents += 1
                continue
            blocked += verified and row.get("disposition") == "blocked"
            effective += verified and row.get("disposition") == "effective"
            suspected += row.get("status") == "suspected"
            unknown_incidents += (not verified or row.get("disposition") not in {"blocked", "effective"}
                                  or (row.get("disposition") == "effective" and not row.get("scope")))
        for row in guard.get("interruptions", {}).get("records", []):
            when = instant(row.get("started_at")) or instant(row.get("occurred_at"))
            if not p.contains(when):
                continue
            recovery["observed"] += 1
            reported, ended = instant(row.get("occurred_at")), instant(row.get("ended_at"))
            if (reported and reported > p.as_of) or (ended and ended > p.as_of):
                recovery["pending"] += 1
                continue
            outcome = row.get("outcome")
            recovery[outcome if outcome in {"resumed", "stopped", "unresolved"} else "unknown"] += 1
            resolved = outcome in {"resumed", "stopped"}
            independently_verified = resolved and row.get("verified_safe") is True and reported is not None and ended is not None
            recovery["verified_safe"] += independently_verified
            recovery["unverified_outcomes"] += resolved and not independently_verified
            elapsed = duration(when, ended, 60)
            if elapsed is not None:
                recovery_minutes.append(elapsed)
    repeated = [dict(requirement=key, status=status, jobs=n)
                for (key, status), n in issues.most_common() if n > 1]
    guard = dict(eligible_requirements=eligible, covered_requirements=covered, proven_requirements=proven,
                 applicable_jobs=applicable_jobs, fully_covered_jobs=fully_covered,
                 jobs_without_requirement_inventory=sum(not j.guard.get("requirements") for j in jobs),
                 gaps=gaps, repeated_gaps=repeated, confirmed_blocked_observed=blocked,
                 confirmed_effective_observed=effective, suspected_observed=suspected,
                 effective_total=effective if jobs and len(jobs) == incident_coverage and not unknown_incidents else None,
                 incident_coverage_jobs=incident_coverage, eligible_jobs=len(jobs), uncertain_incidents=unknown_incidents)
    interrupted = dict(observed=recovery["observed"], resumed=recovery["resumed"],
                       stopped=recovery["stopped"], unresolved=recovery["unresolved"],
                       pending=recovery["pending"], unknown=recovery["unknown"],
                       unverified_outcomes=recovery["unverified_outcomes"],
                       source_coverage=dict(independently_verified=recovery["verified_safe"],
                                            observed_interruptions=recovery["observed"],
                                            eligible_jobs=len(jobs),
                                            jobs_with_complete_observation=sum(
                                                j.covered("observation_coverage", {"interruptions"}, p.start, min(p.end, p.as_of))
                                                for j in jobs)),
                       verified_safe=rate(recovery["verified_safe"], recovery["observed"], recovery["verified_safe"]),
                       elapsed_minutes=sample(recovery_minutes, recovery["observed"]))
    return guard, interrupted


def recovery_report(active, period):
    records = []
    reported_causes = Counter()
    linked_effort = {}
    for job in active:
        trace = recovery_summary(job.raw, job.events)
        for row in trace["decisions"]:
            events = row["observations"]
            if not period.contains(instant(events[0].get("occurred_at"))):
                continue
            if row["reported_cause"]:
                reported_causes[row["reported_cause"]] += 1
            records.append(dict(job=job.id, failure_id=row["failure_id"], cause=row["cause"],
                                reported_cause=row["reported_cause"], outcome=row["outcome"],
                                event_ids=[e["event_id"] for e in events],
                                evidence=[r for e in events for r in e.get("evidence", [])]))
        for row in [*trace["decisions"], *trace["defects"]]:
            for interruption in row["linked_interruptions"]:
                start = instant(interruption.get("started_at"))
                if not start:
                    start = instant(interruption["observations"][0].get("occurred_at"))
                if period.contains(start):
                    for e in row["owner_effort"]:
                        linked_effort[job.id, e["event_id"]] = e["data"].get("minutes")

    amounts = [value for value in linked_effort.values() if value is not None]
    repeated = [dict(cause=k, count=n) for k, n in reported_causes.most_common() if n > 1]
    return (dict(records=records, reported_recurring=repeated, recurring=[]),
            dict(known_owner_minutes=sum(amounts) if amounts else None,
                 linked_effort_entries=len(linked_effort), untimed_effort_entries=len(linked_effort)-len(amounts)))


def task_cohorts(active, period, defect_window_days, allocations, dimensions):
    groups = defaultdict(list)
    for job in active:
        labels = tuple(job.context.get(dimension) for dimension in dimensions)
        if all(labels):
            groups[labels].append(job)
    classified = sum(len(jobs) for jobs in groups.values())
    coverage = dict(covered=classified, eligible=len(active), unknown=len(active)-classified,
                    minimum_jobs=SMALL_SAMPLE, small_groups=[])
    cohorts = []
    for labels, jobs in sorted(groups.items()):
        count = len(jobs)
        cohort = dict(zip(dimensions, labels), jobs=count)
        if count < SMALL_SAMPLE:
            coverage["small_groups"].append(cohort)
            continue
        merged = [j for j in jobs if period.contains(j.merged)]
        effort, full = effort_report(jobs, merged, period)
        delivered = delivery(merged, jobs, period)
        acceptance, states = outcomes(jobs, period)
        guard, interruptions = guardrails(jobs, period)
        recovery, linked_effort = recovery_report(jobs, period)
        interruptions.update(linked_effort)
        totals, cost_coverage = cohort_costs(jobs, allocations)
        cohort.update(merged_jobs=len(merged), lead_hours=delivered["lead_hours"],
                      agent_elapsed_minutes=delivered["agent_elapsed_minutes"], first_gate=delivered["first_gate"],
                      owner_minutes=sample(list(full.values()), count), owner_effort=effort,
                      acceptance=acceptance, outcomes=states, escaped_defects=defects(merged, defect_window_days, period.as_of),
                      guardrails={k: v for k, v in guard.items() if k not in {"gaps", "repeated_gaps"}},
                      interruptions=interruptions, recovery=recovery, escalations=escalations(jobs, period),
                      cost_totals=totals, cost_coverage=cost_coverage)
        cohorts.append(cohort)
    return cohorts, coverage


def matched_tasks(active, full):
    matched = defaultdict(list)
    for job in active:
        key = job.context.get("matched_task_key")
        if key:
            matched[key].append(job)
    pairs = []
    for key, jobs in sorted(matched.items()):
        modes = {j.context.get("workflow_mode"): j for j in jobs}
        if len(jobs) != 2 or set(modes) != {"factory", "direct"}:
            continue
        a, b = modes["factory"], modes["direct"]
        if a.id not in full or b.id not in full:
            continue
        if not all(a.context.get(k) and a.context[k] == b.context.get(k) for k in ("task_type", "task_size")):
            continue
        pairs.append(dict(key=key, factory_job=a.id, direct_job=b.id, factory_minutes=full[a.id],
                          direct_minutes=full[b.id], factory_minus_direct_minutes=full[a.id]-full[b.id]))
    return dict(candidate_keys=len(matched), pairs=pairs, excluded_keys=len(matched)-len(pairs),
                interpretation="Observed differences across comparable recorded tasks.")


def weekly_report(job_records, start, as_of, timezone, defect_window_days):
    """Read-only projection. Pass job, corrected as-of events/diagnostics, and as-of guardrails."""
    first = date.fromisoformat(start) if isinstance(start, str) else start
    if type(first) is not date:
        raise ValueError("weekly start must be a calendar date")
    cutoff = instant(as_of)
    if cutoff is None:
        raise ValueError("as_of must include its time zone")
    if defect_window_days is not None and (type(defect_window_days) is not int or defect_window_days <= 0):
        raise ValueError("defect_window_days must be a positive integer")
    last = first + timedelta(days=7)
    jobs = [Job(row, cutoff) for row in job_records if row.get("origin") != "controlled"
            and row["job"].get("origin") != "controlled"]
    jobs = [j for j in jobs if not j.future or j.events]
    all_kinds = Counter(e["kind"] for j in jobs for e in j.events)
    period = dict(start=str(first), end_exclusive=str(last), timezone=timezone, as_of=stamp(cutoff))
    legacy = {}
    for j in jobs:
        fields = {k: j.raw[k] for k in ("owner_minutes", "cost_usd", "escaped", "owner_changes")
                  if j.raw.get(k) is not None and (k != "owner_minutes" or not j.by_kind["owner_effort"])}
        if fields:
            legacy[j.id] = fields
    result = dict(period=period, defect_window_days=defect_window_days, baseline=BASELINE, legacy=legacy,
                  unwindowed=dict(jobs=len(jobs), observations=dict(all_kinds)),
                  undated_jobs=[j.id for j in jobs if not j.started],
                  undated_statuses={j.id: j.status for j in jobs if not j.started},
                  diagnostics={j.id: j.diagnostics for j in jobs if j.diagnostics},
                  links={j.id: j.url for j in jobs})
    if not timezone:
        result.update(delivered=None, acceptance=None, owner_effort=None, escaped_defects=None,
                      reason="Set the reporting time zone to define the weekly cohort.")
        return result
    zone = ZoneInfo(timezone)
    week = Period(datetime.combine(first, time.min, zone).astimezone(tz.utc),
                  datetime.combine(last, time.min, zone).astimezone(tz.utc), cutoff)
    period.update(start_utc=stamp(week.start), end_utc=stamp(week.end), partial=cutoff < week.end)
    active = [j for j in jobs if j.active(week)]
    worker_rows, omitted = [], 0
    for job in jobs:
        for row in job.record.get("worker_receipts", []):
            if week.contains(instant(row["occurred_at"])):
                if len(worker_rows) < 64:
                    worker_rows.append(row)
                else:
                    omitted += 1
    result["worker_receipts"] = dict(records=worker_rows, omitted=omitted,
                                     diagnostics={j.id: j.record["worker_receipt_diagnostics"] for j in jobs
                                                  if j.record.get("worker_receipt_diagnostics")})
    merged = [j for j in jobs if week.contains(j.merged)]
    effort, full = effort_report(active, merged, week)
    acceptance, states = outcomes(active, week)
    guard, interrupted = guardrails(active, week)
    recovery, linked_effort = recovery_report(active, week)
    interrupted.update(linked_effort)
    weekly_costs, allocations = costs(jobs, active, week)
    dimensions = ("task_type", "task_size")
    cohorts, cohort_coverage = task_cohorts(active, week, defect_window_days, allocations, dimensions)
    by_dimension = {}
    for dimension in dimensions:
        rows, coverage = task_cohorts(active, week, defect_window_days, allocations, (dimension,))
        by_dimension[dimension] = dict(cohorts=rows, coverage=coverage)
    result.update(active_jobs=[j.id for j in active], small_sample=len(active) < SMALL_SAMPLE,
                  delivered=delivery(merged, active, week), acceptance=acceptance, outcomes=states,
                  owner_effort=effort, escaped_defects=defects(merged, defect_window_days, cutoff),
                  costs=weekly_costs, escalations=escalations(active, week), guardrails=guard,
                  interruptions=interrupted, recovery=recovery,
                  task_cohorts=cohorts, task_cohort_coverage=cohort_coverage, task_cohorts_by=by_dimension,
                  matched_tasks=matched_tasks(active, full))
    return result


def render_weekly_report(report):
    def show(value):
        return "unknown" if value is None else str(value)

    def frequency(metric):
        pct = metric["percent"]
        value = f"{pct:g}%" if pct is not None else "rate unavailable"
        return (f"{metric['numerator']}/{metric['eligible']} ({value}, "
                f"{metric['covered']} observed, {metric['unknown']} unknown)")

    def links(ids):
        items = []
        for job in ids:
            if not valid_reporting_job_id(job):
                items.append("Job reference unavailable")
                continue
            href = tracker_url(report["links"].get(job))
            items.append(f"[{job}]({href})" if href else f"`{job}` (local job)")
        return ", ".join(items) or "none"

    p = report["period"]
    lines = [f"# Factory week starting {p['start']}", "",
             f"Through {p['end_exclusive']} (exclusive), {show(p['timezone'])} time zone. "
             f"Evidence as of {p['as_of']}."]
    if report["delivered"] is None:
        counts = ", ".join(f"{n} {k}" for k, n in sorted(report["unwindowed"]["observations"].items()))
        lines += ["", report["reason"], f"Unwindowed inventory covers {report['unwindowed']['jobs']} jobs. "
                  f"Recorded observations · {counts or 'none'}.", "", report["baseline"]]
        return "\n".join(lines) + "\n"
    d, a, e = report["delivered"], report["acceptance"], report["owner_effort"]
    g, r, s = report["guardrails"], report["interruptions"], report["escalations"]
    if p["partial"]:
        lines += ["", "Partial week at this extraction time."]
    lines += ["", f"**Delivered** {d['count']} merged. {links(d['jobs'])}.",
              f"Median lead time {show(d['lead_hours']['median'])} hours "
              f"({d['lead_hours']['sample']}/{d['count']} timed). "
              f"Median elapsed time to ready {show(d['agent_elapsed_minutes']['median'])} minutes "
              f"({d['agent_elapsed_minutes']['sample']}/{d['count']} timed). "
              f"First gate passed on {frequency(d['first_gate'])}.",
              "", f"**Owner outcome** Accepted without material follow-up on {frequency(a['rate'])}. "
              f"Accepted with follow-up {a['accepted_with_follow_up']}, rejected {a['rejected']}, "
              f"abandoned {a['abandoned']}, failed {a['failed']}, pending {a['pending']}, "
              f"unobserved {a['unobserved']}.",
              f"Active job outcomes · {', '.join(f'{markdown_text(k)} {n}' for k, n in sorted(report['outcomes'].items())) or 'none'}. "
              f"Terminal jobs with no recorded submission {a['no_recorded_submission']}. "
              f"Other jobs missing a submission time {a['submission_unobserved']}.",
              "", f"**Owner time** {show(e['known_minutes'])} known active minutes, "
              f"{e['covered_jobs']}/{e['eligible_jobs']} jobs fully covered. "
              f"Preparation {show(e['activities']['preparation'])}, correction "
              f"{show(e['activities']['correction'])}, known rework subtotal {show(e['rework_minutes'])} minutes "
              f"(total {show(e['rework_total_minutes'])}). "
              f"Unclassified rework status on {e['unknown_rework_entries']} entries "
              f"({e['unclassified_minutes']} known minutes), {e['unknown_rework_amount_entries']} possible rework "
              f"entries missing minutes. "
              f"Median per merged PR {show(e['per_merge_median']['minutes'])} minutes "
              f"({e['per_merge_median']['sample']} fully measured, {e['per_merge_median']['excluded']} excluded)."]
    defect = report["escaped_defects"]
    if defect["rate"] is None:
        lines += ["", f"**Escaped defects** {defect['reason']} "
                  f"{defect['unwindowed_observations']} defect observations on delivered jobs."]
    else:
        lines += ["", f"**Escaped defects** {frequency(defect['rate'])} mature merges in a "
                  f"{defect['window_days']}-day follow-up window. {defect['immature']} still maturing."]
    lines += ["", f"**Escalations** Necessary among owner-rated requests · {frequency(s['precision'])}. "
              f"{s['unrated']} unrated and {s['missed']} missed decisions observed. "
              f"Request coverage {s['covered_jobs']}/{s['eligible_jobs']} jobs.",
              f"**Recovery** Verified safe outcomes · {frequency(r['verified_safe'])}. "
              f"{r['resumed']} resumed, {r['stopped']} stopped, {r['unresolved']} unresolved, "
              f"{r['pending']} pending, {r['unknown']} unknown. "
              f"{r['unverified_outcomes']} reported resolution{'s' if r['unverified_outcomes'] != 1 else ''} "
              f"without independent verification. Linked owner time "
              f"{show(r['known_owner_minutes'])} known minutes ({r['linked_effort_entries']} entries). "
              f"Median elapsed {show(r['elapsed_minutes']['median'])} minutes "
              f"({r['elapsed_minutes']['sample']} timed).",
              "", f"**Guardrails at extraction** {g['covered_requirements']}/{g['eligible_requirements']} "
              f"applicable requirements verified, {g['proven_requirements']} passing. "
              f"{g['jobs_without_requirement_inventory']} jobs lack a requirement inventory. "
              f"Confirmed blocked attempts observed {g['confirmed_blocked_observed']}, "
              f"suspected incidents observed {g['suspected_observed']}. Effective violations "
              f"{show(g['effective_total'])} ({g['confirmed_effective_observed']} confirmed, "
              f"{g['incident_coverage_jobs']}/{g['eligible_jobs']} jobs covered)."]
    worker = report["worker_receipts"]
    if worker["records"] or worker["diagnostics"]:
        lines += ["", "**Unverified worker receipts**"]
        for row in worker["records"][:8]:
            lines.append(f"- {links([row['job_id']])} · round {row['round']} · {row['kind']} · "
                         f"{row['occurred_at']} · {row['origin']}")
        if len(worker["records"]) > 8 or worker["omitted"]:
            lines.append(f"- {len(worker['records']) + worker['omitted'] - 8} additional local records.")
        if worker["diagnostics"]:
            lines.append("Worker receipt inventory is incomplete. See the JSON diagnostics.")
    for gap in g["gaps"][:8]:
        job_id = gap["job"]
        trusted_urls = (tracker_url(report["links"].get(job_id)),)
        refs = [evidence_reference(ref, job_id, trusted_urls=trusted_urls) for ref in gap["evidence"]]
        references = [ref for ref in refs if ref]
        if not references:
            references = [ref for path in gap["legacy_paths"]
                          if (ref := evidence_reference({"path": path}, job_id, label="Historical evidence"))]
        if not references and (gap["evidence"] or gap["legacy_paths"]):
            references = ["Evidence link unavailable"]
        detail = " · ".join([links([job_id]), markdown_text(gap["requirement"]), gap["status"], *references])
        lines.append(f"- {detail}")
    if len(g["gaps"]) > 8:
        lines.append(f"- {len(g['gaps'])-8} more requirement gaps in the JSON report.")
    repeated = [f"{markdown_text(i['requirement'])} {i['status']} on {i['jobs']} jobs" for i in g["repeated_gaps"]]
    lines += ["", "**Recurring bottlenecks** " + (", ".join(repeated) or "No repeated requirement gap observed.")]

    for cause in report["recovery"]["reported_recurring"]:
        affected = [r for r in report["recovery"]["records"] if r["reported_cause"] == cause["cause"]]
        lines.append(f"- Reported cause {markdown_text(cause['cause'])} (unverified) · {cause['count']} distinct failures on "
                     f"{links(dict.fromkeys(r['job'] for r in affected))}.")
        for row in affected[:3]:
            job_id = row["job"]
            trusted_urls = (tracker_url(report["links"].get(job_id)),)
            for ref in row["evidence"][:1]:
                reference = evidence_reference(ref, job_id, trusted_urls=trusted_urls)
                lines.append(f"  - {markdown_text(row['failure_id'])} · {reference or 'Evidence link unavailable'}")

    if report["links"]:
        lines += ["", "On the Factory host, run `factory/bin/job card <job-id>` from the repository root "
                  "to inspect a local job and its evidence references."]
    rows = report["costs"]["totals"]
    lines += ["", "**Usage and cost**"]
    for kind in COST_TYPES:
        values = [f"{show(row['amount'])} {row['currency'] or row['unit']}" for row in rows if row["cost_type"] == kind]
        cov = report["costs"]["coverage"][kind]
        lines.append(f"- {kind.replace('_', ' ').capitalize()} · {', '.join(values) or 'unknown'} "
                     f"({cov['jobs_with_amount']}/{cov['eligible_jobs']} active jobs and "
                     f"{cov['other_jobs_with_amount']} other jobs with recorded amounts).")
    if report["costs"]["unattributed"]:
        lines.append(f"{len(report['costs']['unattributed'])} usage or billing observations could not be attributed.")
    if report["legacy"]:
        lines += ["", "Legacy values read at extraction (weekly coverage and cost type unknown) · " +
                  ", ".join(f"{links([key])} " + ", ".join(f"{k}={v}" for k, v in row.items())
                            for key, row in report["legacy"].items()) + "."]
    if report["undated_jobs"]:
        lines += ["", "Missing start time · " + ", ".join(f"{links([key])} ({markdown_text(show(stage))})"
                      for key, stage in report["undated_statuses"].items()) + "."]
    if report["diagnostics"]:
        lines += ["", "Incomplete observation files · " + links(report["diagnostics"]) + "."]
    lines += ["", "**Task type and size**",
              "Each view groups the same active jobs. Counts across views overlap. Groups and individual "
              f"measures with fewer than {SMALL_SAMPLE} observations are small samples."]
    views = [(("task_type", "task_size"), "Type and size", "both labels",
              report["task_cohorts"], report["task_cohort_coverage"])]
    for dimension, view in report["task_cohorts_by"].items():
        label = "Type" if dimension == "task_type" else "Size"
        views.append(((dimension,), label, f"a {label.lower()} label", view["cohorts"], view["coverage"]))
    for dimensions, title, classification, rows, coverage in views:
        def label_for(row):
            return " / ".join(markdown_text(row[d]) for d in dimensions)

        lines += ["", f"{title} · {coverage['covered']}/{coverage['eligible']} active jobs have {classification} "
                  f"({coverage['unknown']} unknown)."]
        for c in rows[:2]:
            lead, elapsed, effort = c["lead_hours"], c["agent_elapsed_minutes"], c["owner_effort"]
            guard, owner = c["guardrails"], c["owner_minutes"]
            lines.append(f"- {label_for(c)} · {c['jobs']} active, {c['merged_jobs']} merged. "
                         f"Accepted as built {frequency(c['acceptance']['rate'])}. "
                         f"Median lead {show(lead['median'])} hours ({lead['sample']}/{c['merged_jobs']} timed, "
                         f"{lead['excluded']} unknown). Median elapsed to ready {show(elapsed['median'])} minutes "
                         f"({elapsed['sample']}/{c['merged_jobs']} timed, {elapsed['excluded']} unknown).")
            lines.append(f"  Owner active time {show(effort['known_minutes'])} known minutes "
                         f"({effort['covered_jobs']}/{c['jobs']} fully covered, {effort['unknown_jobs']} unknown). "
                         f"Median whole-job owner time {show(owner['median'])} minutes "
                         f"({owner['sample']}/{c['jobs']} measured, {owner['excluded']} unknown). "
                         f"Rework {show(effort['rework_minutes'])} known minutes "
                         f"({effort['rework_covered_jobs']}/{c['jobs']} fully covered, "
                         f"{c['jobs']-effort['rework_covered_jobs']} unknown). "
                         f"Guardrails {guard['covered_requirements']}/{guard['eligible_requirements']} applicable "
                         f"requirements verified, {guard['proven_requirements']} passing, "
                         f"{guard['eligible_requirements']-guard['covered_requirements']} unknown. "
                         f"{guard['jobs_without_requirement_inventory']} jobs lack an inventory.")
        small = coverage["small_groups"]
        if small:
            labels = [f"{label_for(c)} ({c['jobs']})" for c in small[:4]]
            lines += ["", f"Small sample groups, by active job count · {', '.join(labels)}."]
        omitted = max(0, len(rows)-2) + max(0, len(small)-4)
        if omitted:
            lines.append(f"{omitted} additional groups appear in the JSON report.")
    if any(rows for _, _, _, rows, _ in views):
        lines += ["", "The JSON report includes group outcomes, defect follow-up, escalations, recovery and "
                  "costs. Cost subtotals include uniquely attributable recorded amounts. Shared and conflicting "
                  "items are counted separately."]
    m = report["matched_tasks"]
    if m["candidate_keys"]:
        lines += ["", f"**Matched tasks** {len(m['pairs'])}/{m['candidate_keys']} keys comparable. "
                  "Observed owner-minute differences (factory minus direct) · " +
                  (", ".join(f"{markdown_text(i['key'])} {i['factory_minus_direct_minutes']:+g}" for i in m["pairs"]) or "unknown") + "."]
    if report["small_sample"]:
        lines += ["", f"Small sample ({len(report['active_jobs'])} active jobs, threshold {SMALL_SAMPLE})."]
    return "\n".join(lines + ["", report["baseline"]]) + "\n"
