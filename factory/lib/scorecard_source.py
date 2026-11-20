"""Collect the repository-scoped inputs shared by the weekly scorecard and export."""

import argparse
import os
import stat
from contextlib import ExitStack
from datetime import date, datetime, timedelta

import factory as F
from evidence_adapter import reporting_record
from observations import _open_directory
from scorecard_report import weekly_report


def weekly_start(value):
    try:
        start = date.fromisoformat(value)
        if start.isoformat() == value and start <= date.max - timedelta(days=7):
            return start
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("expected a seven-day interval starting YYYY-MM-DD")


def utc_cutoff(value):
    try:
        cutoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if cutoff.utcoffset() == timedelta(0):
            return cutoff
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("expected a UTC timestamp, e.g. 2026-11-19T09:00:00Z")


def _weekly_folders(store):
    folders = []
    with _open_directory(store.parent) as parent, ExitStack() as stack:
        try:
            job_store = stack.enter_context(_open_directory(store.name, parent))
        except FileNotFoundError:
            return []
        for name in os.listdir(job_store):
            try:
                entry = os.stat(name, dir_fd=job_store, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
                folders.append(store / name)
        current = os.stat(store.name, dir_fd=parent, follow_symlinks=False)
        if not os.path.samestat(current, os.fstat(job_store)):
            raise F.FactoryError("job store changed during weekly reporting")
    return folders


def read_jobs(root, read, *, include_unreadable_directories=False):
    records, unreadable, invalid_ids = [], {}, 0
    store = F.jobs_dir(root)
    if include_unreadable_directories:
        folders = _weekly_folders(store)
    else:
        folders = [path.parent for path in store.glob("*/job.json")]
    for folder in sorted(folders):
        job_id = folder.name
        if not F.valid_reporting_job_id(job_id):
            invalid_ids += 1
            continue
        try:
            records.append(read(F.reporting_job_dir(root, job_id)))
        except F.FactoryError as exc:
            unreadable[job_id] = [str(exc)]
        except (OSError, ValueError, RecursionError):
            unreadable[job_id] = [f"invalid or unreadable job state for {job_id}"]
    return records, unreadable, invalid_ids


def collect_weekly(root, cfg, start, as_of, timezone, defect_window_days, *,
                   verify_github_links=False, verify_github_merges=False):
    records, unreadable, invalid_ids = read_jobs(
        root, lambda folder: reporting_record(root, cfg, folder, as_of=as_of, verify_github_links=verify_github_links),
        include_unreadable_directories=True,
    )
    verifications = {}
    if verify_github_merges:
        from merge_evidence import verify_merge_delivery

        for record in records:
            result = verify_merge_delivery(root, cfg, record, as_of)
            if result is not None:
                verifications[record["job"]["id"]] = result
    report = weekly_report(records, start, as_of, timezone, defect_window_days)
    report["diagnostics"].update(unreadable)
    report["invalid_job_directories"] = invalid_ids
    report["incident_and_interruption_source_verification"] = "unavailable"
    if verify_github_merges:
        report["merge_verification"] = verifications
    controlled = sum(record.get("origin") == "controlled" or record["job"].get("origin") == "controlled"
                     for record in records)
    return report, unreadable, controlled
