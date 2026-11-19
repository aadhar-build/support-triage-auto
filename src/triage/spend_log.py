"""Durable monthly spend log for paid model calls, kept outside the repository.

The log is UTF-8 JSON lines, append-only. The first line is an `open` event written only
by SpendLog.create; every later line is a `reserve`, `settle` or `unknown` event. Before
each paid call the caller reserves the worst-case cost under an exclusive flock; the
reservation stays charged until it is settled at its real cost. An unsettled reservation
(a crash) or one marked `unknown` keeps its reserved amount. A missing or corrupt log is
an error, never an empty month: every method fails closed and writes nothing.

The log holds no thread id, text, author id or brand id.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Mapping, Optional

from triage import trace

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")
_FIELDS = {
    "open": ("event", "id", "at"),
    "reserve": ("event", "id", "at", "run_id", "model", "split", "usd"),
    "settle": ("event", "id", "at", "usd") + TOKEN_KEYS,
    "unknown": ("event", "id", "at"),
}
SPLITS = ("dev", "eval")


class SpendLogError(ValueError):
    """The spend log or the thresholds file is missing, corrupt or refused."""


class MonthlyCapError(SpendLogError):
    """A reservation would take month-to-date spend above the monthly cap."""


def load_monthly_cap(path) -> float:
    """Read guardrails.api_spend_usd_per_month.max from a thresholds-shaped file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise SpendLogError("thresholds file %s not found" % path) from None
    except OSError as e:
        raise SpendLogError("cannot read thresholds file %s: %s" % (path, e.strerror or e)) \
            from None
    except (ValueError, UnicodeDecodeError):
        raise SpendLogError("thresholds file %s is not valid JSON" % path) from None
    try:
        value = data["guardrails"]["api_spend_usd_per_month"]["max"]
    except (KeyError, TypeError):
        raise SpendLogError("thresholds file %s lacks guardrails.api_spend_usd_per_month.max"
                            % path) from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        raise SpendLogError("thresholds file %s: api_spend_usd_per_month.max must be a finite "
                            "number > 0" % path)
    return float(value)


def _check_path(path) -> str:
    try:
        trace.check_trace_path(path)
    except trace.TraceError:
        raise SpendLogError("spend log path %s is inside the repository %s; keep it outside"
                            % (os.path.abspath(str(path)), trace.REPO_ROOT)) from None
    return str(path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _amount(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) \
        and math.isfinite(value) and value >= 0


def _event(name: str, at: datetime, **fields) -> str:
    rec = {"event": name, "id": fields.pop("id", None) or uuid.uuid4().hex,
           "at": at.astimezone(timezone.utc).isoformat()}
    rec.update(fields)
    return json.dumps(rec, sort_keys=True) + "\n"


class SpendLog:
    def __init__(self, path, monthly_cap_usd: float,
                 clock: Optional[Callable[[], datetime]] = None):
        self.path = _check_path(path)
        self.monthly_cap_usd = monthly_cap_usd
        self._clock = clock or _now

    @classmethod
    def create(cls, path) -> None:
        """Write the `open` line to a new log. Refuses an existing file."""
        p = _check_path(path)
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SpendLogError("spend log %s already exists; refusing to recreate it" % p) \
                from None
        except OSError as e:
            raise SpendLogError("cannot create spend log %s: %s" % (p, e.strerror or e)) \
                from None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_event("open", _now()))
            f.flush()
            os.fsync(f.fileno())

    # -- locked file access -------------------------------------------------------------

    def _open_locked(self) -> int:
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND)
        except FileNotFoundError:
            raise SpendLogError("spend log %s is missing; a missing log is never an empty "
                                "month (create one with --create-spend-log)" % self.path) \
                from None
        except OSError as e:
            raise SpendLogError("cannot open spend log %s: %s" % (self.path, e.strerror or e)) \
                from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            os.close(fd)
            raise SpendLogError("cannot lock spend log %s: %s" % (self.path, e.strerror or e)) \
                from None
        return fd

    def _read(self, fd: int) -> List[dict]:
        chunks = []
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        return _validate(b"".join(chunks), self.path)

    def _append(self, fd: int, text: str) -> None:
        data = text.encode("utf-8")
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)

    def _with_lock(self, fn):
        fd = self._open_locked()
        try:
            return fn(fd)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # -- public API ---------------------------------------------------------------------

    def _mtd(self, events: List[dict]) -> float:
        now = self._clock().astimezone(timezone.utc)
        reserved: Dict[str, float] = {}
        final: Dict[str, float] = {}
        for e in events:
            if e["event"] == "reserve":
                at = datetime.fromisoformat(e["at"]).astimezone(timezone.utc)
                if (at.year, at.month) == (now.year, now.month):
                    reserved[e["id"]] = e["usd"]
            elif e["event"] == "settle":
                final[e["id"]] = e["usd"]
        return sum(final.get(rid, usd) for rid, usd in reserved.items())

    def month_to_date(self) -> float:
        return self._with_lock(lambda fd: self._mtd(self._read(fd)))

    def reserve(self, run_id: str, model: str, split: str, usd: float) -> str:
        if split not in SPLITS:
            raise SpendLogError("split must be one of %s" % ", ".join(SPLITS))
        if not _amount(usd):
            raise SpendLogError("reservation must be a finite number >= 0")

        def do(fd):
            mtd = self._mtd(self._read(fd))
            if mtd + usd > self.monthly_cap_usd:
                raise MonthlyCapError("monthly cap %.4f reached: month to date %.4f plus "
                                      "%.4f" % (self.monthly_cap_usd, mtd, usd))
            rid = uuid.uuid4().hex
            self._append(fd, _event("reserve", self._clock(), id=rid, run_id=run_id,
                                    model=model, split=split, usd=usd))
            return rid
        return self._with_lock(do)

    def _close(self, rid: str, line: str) -> None:
        def do(fd):
            events = self._read(fd)
            if rid not in {e["id"] for e in events if e["event"] == "reserve"}:
                raise SpendLogError("no reservation with that id")
            if rid in {e["id"] for e in events if e["event"] in ("settle", "unknown")}:
                raise SpendLogError("reservation already settled or marked unknown")
            self._append(fd, line)
        self._with_lock(do)

    def settle(self, rid: str, usage_usd: float, tokens: Mapping[str, int]) -> None:
        if not _amount(usage_usd):
            raise SpendLogError("settled usd must be a finite number >= 0")
        counts = {k: tokens.get(k, 0) for k in TOKEN_KEYS}
        if not all(_amount(v) and isinstance(v, int) for v in counts.values()):
            raise SpendLogError("token counts must be non-negative ints")
        self._close(rid, _event("settle", self._clock(), id=rid, usd=usage_usd, **counts))

    def mark_unknown(self, rid: str) -> None:
        self._close(rid, _event("unknown", self._clock(), id=rid))


def _validate(raw: bytes, path: str) -> List[dict]:
    """Parse and check every line. Errors name the line number, never its content."""
    def fail(n, problem):
        raise SpendLogError("spend log %s line %d: %s" % (path, n, problem))

    if raw and not raw.endswith(b"\n"):
        fail(raw.count(b"\n") + 1, "partial last line")
    lines = raw.split(b"\n")[:-1] if raw else []
    if not lines:
        fail(1, "missing open line")
    events: List[dict] = []
    reserved, closed = set(), set()
    for n, b in enumerate(lines, start=1):
        try:
            e = json.loads(b.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            fail(n, "not valid JSON")
        if not isinstance(e, dict):
            fail(n, "not an object")
        name = e.get("event")
        if name not in _FIELDS:
            fail(n, "unknown event")
        if (n == 1) != (name == "open"):
            fail(n, "missing open line" if n == 1 else "open event after line 1")
        for key in _FIELDS[name]:
            if key not in e:
                fail(n, "missing field %s" % key)
        if not isinstance(e["id"], str) or not e["id"]:
            fail(n, "bad id")
        try:
            at = datetime.fromisoformat(e["at"])
        except (TypeError, ValueError):
            fail(n, "bad timestamp")
        if at.tzinfo is None:
            fail(n, "timestamp has no time zone")
        for key in ("usd",) + TOKEN_KEYS:
            if key in _FIELDS[name] and not _amount(e[key]):
                fail(n, "%s must be a finite number >= 0" % key)
        if name == "reserve":
            if e["id"] in reserved:
                fail(n, "duplicate reservation id")
            if e["split"] not in SPLITS:
                fail(n, "bad split")
            reserved.add(e["id"])
        elif name in ("settle", "unknown"):
            if e["id"] not in reserved:
                fail(n, "%s without a reservation" % name)
            if e["id"] in closed:
                fail(n, "second settle or unknown for one reservation")
            closed.add(e["id"])
        events.append(e)
    return events
