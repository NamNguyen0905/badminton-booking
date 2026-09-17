#!/usr/bin/env python3
"""Metrics Lambda for FeatherFlight booking dashboard.

Reads booking event JSONL files from S3 and returns aggregated metrics.
Password-protected via METRICS_PASSWORD environment variable.

GET /metrics?password=xxx              → full dashboard payload
GET /metrics?password=xxx&raw=1        → raw event list (last 500)
GET /metrics?password=xxx&range=30d    → date range (default 30d)
GET /metrics?password=xxx&range=all    → all events
"""

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import boto3

EVENTS_S3_BUCKET = os.environ.get("EVENTS_S3_BUCKET", "")
EVENTS_S3_PREFIX = os.environ.get("EVENTS_S3_PREFIX", "metrics/events/")
METRICS_PASSWORD = os.environ.get("METRICS_PASSWORD", "")
CORS_ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")

_s3 = None


def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _cors():
    return {
        "Access-Control-Allow-Origin": CORS_ORIGIN,
        "Access-Control-Allow-Headers": "content-type",
        "Access-Control-Allow-Methods": "GET,OPTIONS",
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
    }


def _resp(code, body):
    return {"statusCode": code, "headers": _cors(), "body": json.dumps(body)}


def _parse_range(value):
    """Return (start_date, end_date) from a range string like '7d', '30d', 'all'."""
    today = datetime.now(timezone.utc).date()
    if value == "all":
        return None, today  # None means no lower bound
    days = 30
    if value and value.endswith("d"):
        try:
            days = int(value[:-1])
        except ValueError:
            pass
    return today - timedelta(days=days), today


def _load_events(start_date, end_date):
    """Load events from S3 for the given date range."""
    s3 = _s3_client()
    events = []

    # List date-partitioned prefixes
    if start_date is None:
        # All events — list all year prefixes
        prefixes = []
        resp = s3.list_objects_v2(Bucket=EVENTS_S3_BUCKET, Prefix=EVENTS_S3_PREFIX, Delimiter="/")
        for p in resp.get("CommonPrefixes", []):
            prefixes.append(p["Prefix"])
        # Paginate
        while resp.get("IsTruncated"):
            resp = s3.list_objects_v2(
                Bucket=EVENTS_S3_BUCKET, Prefix=EVENTS_S3_PREFIX, Delimiter="/",
                ContinuationToken=resp["NextContinuationToken"],
            )
            prefixes.extend(p["Prefix"] for p in resp.get("CommonPrefixes", []))
    else:
        # Generate year/month/day prefixes for the range
        prefixes = []
        d = start_date
        while d <= end_date:
            prefixes.append(f"{EVENTS_S3_PREFIX}{d:%Y/%m/%d}/")
            d += timedelta(days=1)

    for prefix in prefixes:
        resp = s3.list_objects_v2(Bucket=EVENTS_S3_BUCKET, Prefix=prefix)
        for obj in resp.get("Contents", []):
            try:
                body = s3.get_object(Bucket=EVENTS_S3_BUCKET, Key=obj["Key"])["Body"].read()
                events.append(json.loads(body))
            except Exception:
                continue
        # Paginate if needed
        while resp.get("IsTruncated"):
            resp = s3.list_objects_v2(
                Bucket=EVENTS_S3_BUCKET, Prefix=prefix,
                ContinuationToken=resp["NextContinuationToken"],
            )
            for obj in resp.get("Contents", []):
                try:
                    body = s3.get_object(Bucket=EVENTS_S3_BUCKET, Key=obj["Key"])["Body"].read()
                    events.append(json.loads(body))
                except Exception:
                    continue

    return events


def _classify_failure(event):
    """Extract a human-readable failure reason from an event."""
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
        # Check per-person results
        results = event.get("results", [])
        reasons = []
        for r in results:
            if not r.get("success"):
                msg = r.get("message", "unknown")
                if _is_already_booked_result(r):
                    continue
                if "membership" in msg.lower():
                    reasons.append("Membership not found")
                elif "already" in msg.lower():
                    reasons.append("Already reserved")
                elif "reserve" in msg.lower() or "register" in msg.lower():
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
    status = event.get("status")
    if status == "success":
        return True
    results = _effective_results(event)
    return bool(results) and all(r.get("success") for r in results)


def _aggregate(events):
    """Aggregate events into dashboard-ready metrics."""
    if not events:
        return {
            "total": 0,
            "success_count": 0,
            "failure_count": 0,
            "success_rate": 0,
            "avg_duration_ms": 0,
            "by_email": {},
            "by_day": {},
            "by_slot": {},
            "by_source": {},
            "failure_reasons": {},
            "recent": [],
        }

    total = len(events)
    success = sum(1 for e in events if _effective_is_success(e))
    failure = total - success

    by_email = defaultdict(lambda: {"attempts": 0, "success": 0, "failure": 0})
    by_day = defaultdict(lambda: {"attempts": 0, "success": 0, "failure": 0})
    by_slot = defaultdict(lambda: {"attempts": 0, "success": 0, "failure": 0})
    by_source = Counter()
    failure_reasons = Counter()
    durations = []

    for e in events:
        email = e.get("email", "unknown")
        is_success = _effective_is_success(e)
        by_email[email]["attempts"] += 1
        by_email[email]["success" if is_success else "failure"] += 1

        day = e.get("date", e.get("timestamp", "")[:10])
        by_day[day]["attempts"] += 1
        by_day[day]["success" if is_success else "failure"] += 1

        slot = e.get("time", "unknown")
        by_slot[slot]["attempts"] += 1
        by_slot[slot]["success" if is_success else "failure"] += 1

        by_source[e.get("source", "unknown")] += 1

        if not is_success:
            reason = _classify_failure(e)
            if reason:
                failure_reasons[reason] += 1

        dur = e.get("duration_ms")
        if dur is not None:
            durations.append(dur)

    # Sort recent by timestamp descending, take last 50
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

    # Sort by_day chronologically
    by_day_sorted = dict(sorted(by_day.items()))

    return {
        "total": total,
        "success_count": success,
        "failure_count": failure,
        "success_rate": round(success / total * 100, 1) if total else 0,
        "avg_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
        "by_email": dict(by_email),
        "by_day": by_day_sorted,
        "by_slot": dict(by_slot),
        "by_source": dict(by_source),
        "failure_reasons": dict(failure_reasons),
        "recent": recent_slim,
    }


def lambda_handler(event, context):
    method = (event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "GET").upper()

    if method == "OPTIONS":
        return _resp(204, {})

    qs = event.get("queryStringParameters") or {}
    password = qs.get("password", "")

    if not METRICS_PASSWORD:
        return _resp(500, {"ok": False, "message": "METRICS_PASSWORD not configured."})
    if password != METRICS_PASSWORD:
        return _resp(401, {"ok": False, "message": "Invalid password."})

    if not EVENTS_S3_BUCKET:
        return _resp(500, {"ok": False, "message": "EVENTS_S3_BUCKET not configured."})

    range_val = qs.get("range", "30d")
    start_date, end_date = _parse_range(range_val)

    try:
        events = _load_events(start_date, end_date)
    except Exception as exc:
        print(f"Error loading events: {exc}")
        return _resp(500, {"ok": False, "message": f"Error loading events: {exc}"})

    if qs.get("raw") == "1":
        # Return raw events (limited to 500)
        raw = sorted(events, key=lambda e: e.get("timestamp", ""), reverse=True)[:500]
        return _resp(200, {"ok": True, "count": len(raw), "events": raw})

    agg = _aggregate(events)
    return _resp(200, {"ok": True, "range": range_val, **agg})
