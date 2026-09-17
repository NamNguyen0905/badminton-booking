"""Dashboard aggregation over the booking events file. Ported from the metrics Lambda."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def parse_range(value):
    """Return (start_date, end_date) from a range string like '7d', '30d', 'all'."""
    today = datetime.now(timezone.utc).date()
    if value == "all":
        return None, today
    days = 30
    if value and value.endswith("d"):
        try:
            days = int(value[:-1])
        except ValueError:
            pass
    return today - timedelta(days=days), today


def load_events(path: Path, start_date, end_date):
    if not path.exists():
        return []
    events = []
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if start_date is not None:
                day = (event.get("timestamp") or "")[:10]
                if not day or not (start_date.isoformat() <= day <= end_date.isoformat()):
                    continue
            events.append(event)
    return events


def _classify_failure(event):
    status = event.get("status", "")
    if status == "success":
        return None
    if status == "login_failed":
        return "Login failed"
    if status == "slot_not_found":
        return "Slot not found"
    if status == "validation_error":
        return "Validation error"
    if status == "server_error":
        return "Server error"
    if status == "partial_or_failed":
        reasons = []
        for r in event.get("results", []):
            if not r.get("success"):
                msg = r.get("message", "unknown")
                if _is_already_booked_result(r):
                    continue
                if "membership" in msg.lower():
                    reasons.append("Membership not found")
                elif "already" in msg.lower():
                    reasons.append("Already reserved")
                elif "waitlist" in msg.lower():
                    reasons.append("Waitlisted only")
                elif "did not appear" in msg.lower():
                    reasons.append("Not registered after request")
                elif "reserve" in msg.lower() or "register" in msg.lower() or "rejected" in msg.lower():
                    reasons.append("Reservation rejected")
                else:
                    reasons.append(msg[:80])
        return "; ".join(reasons) if reasons else None
    if status == "person_id_failed":
        return "Account discovery failed"
    if status == "started":
        return "Incomplete (no response)"
    return status or "Unknown"


def _is_already_booked_result(result):
    msg = (result.get("message") or "").lower()
    return "already have reserved or waitlisted" in msg or "already reserved" in msg


def _effective_results(event):
    normalized = []
    for result in event.get("results") or []:
        item = dict(result)
        if not item.get("success") and _is_already_booked_result(item):
            item["success"] = True
            item["message"] = "already booked"
            item["status"] = "already_booked"
        normalized.append(item)
    return normalized


def _effective_is_success(event):
    if event.get("status") == "success":
        return True
    results = _effective_results(event)
    return bool(results) and all(r.get("success") for r in results)


def aggregate(events):
    if not events:
        return {
            "total": 0, "success_count": 0, "failure_count": 0, "success_rate": 0, "avg_duration_ms": 0,
            "by_email": {}, "by_day": {}, "by_slot": {}, "by_source": {}, "failure_reasons": {}, "recent": [],
        }

    total = len(events)
    success = sum(1 for e in events if _effective_is_success(e))

    by_email = defaultdict(lambda: {"attempts": 0, "success": 0, "failure": 0})
    by_day = defaultdict(lambda: {"attempts": 0, "success": 0, "failure": 0})
    by_slot = defaultdict(lambda: {"attempts": 0, "success": 0, "failure": 0})
    by_source = Counter()
    failure_reasons = Counter()
    durations = []

    for e in events:
        is_success = _effective_is_success(e)
        key = "success" if is_success else "failure"
        by_email[e.get("email", "unknown")]["attempts"] += 1
        by_email[e.get("email", "unknown")][key] += 1
        day = e.get("date", e.get("timestamp", "")[:10])
        by_day[day]["attempts"] += 1
        by_day[day][key] += 1
        by_slot[e.get("time", "unknown")]["attempts"] += 1
        by_slot[e.get("time", "unknown")][key] += 1
        by_source[e.get("source", "unknown")] += 1
        if not is_success:
            reason = _classify_failure(e)
            if reason:
                failure_reasons[reason] += 1
        if e.get("duration_ms") is not None:
            durations.append(e["duration_ms"])

    recent = sorted(events, key=lambda e: e.get("timestamp", ""), reverse=True)[:50]
    recent_slim = []
    for e in recent:
        effective_results = _effective_results(e)
        is_success = _effective_is_success(e)
        if effective_results:
            success_count = sum(1 for r in effective_results if r.get("success"))
            failure_count = len(effective_results) - success_count
        else:
            success_count = e.get("success_count")
            failure_count = e.get("failure_count")
        recent_slim.append({
            "timestamp": e.get("timestamp", ""),
            "email": e.get("email", ""),
            "date": e.get("date", ""),
            "time": e.get("time", ""),
            "status": "success" if is_success else e.get("status", ""),
            "source": e.get("source", ""),
            "success_count": success_count,
            "failure_count": failure_count,
            "failure_reason": _classify_failure(e) if not is_success else None,
            "duration_ms": e.get("duration_ms"),
            "results": effective_results or e.get("results"),
        })

    return {
        "total": total,
        "success_count": success,
        "failure_count": total - success,
        "success_rate": round(success / total * 100, 1) if total else 0,
        "avg_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
        "by_email": dict(by_email),
        "by_day": dict(sorted(by_day.items())),
        "by_slot": dict(by_slot),
        "by_source": dict(by_source),
        "failure_reasons": dict(failure_reasons),
        "recent": recent_slim,
    }
