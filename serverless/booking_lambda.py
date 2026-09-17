#!/usr/bin/env python3
"""Minimal Streamlit UI for booking FeatherFlight slots.

Users enter ZenPlanner credentials, date, and time. The app logs in, discovers
account IDs, and books the requested slot for all family members on the account.
"""

import re
import json
import os
import time
import uuid as _uuid
from datetime import date
from datetime import datetime, timezone
from pathlib import Path

import boto3
import cloudscraper
from bs4 import BeautifulSoup


BASE_URL = "https://featherflightbadminton.sites.zenplanner.com"
TIMES = ["4:00 PM", "5:00 PM", "6:00 PM", "7:00 PM", "8:00 PM", "9:00 PM"]
RUNTIME_DIR = Path(os.environ.get("FEATHERFLIGHT_RUNTIME_DIR", "/tmp/featherflight"))
TRAFFIC_LOG = RUNTIME_DIR / "traffic.jsonl"
MEMBERSHIP_CACHE_FILE = RUNTIME_DIR / "membership_cache.json"
EVENTS_S3_BUCKET = os.environ.get("EVENTS_S3_BUCKET", "")
EVENTS_S3_PREFIX = os.environ.get("EVENTS_S3_PREFIX", "metrics/events/")
_s3_client = None


def load_membership_cache():
    if not MEMBERSHIP_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(MEMBERSHIP_CACHE_FILE.read_text())
    except Exception:
        return {}


def save_membership_cache(cache):
    RUNTIME_DIR.mkdir(exist_ok=True)
    MEMBERSHIP_CACHE_FILE.write_text(json.dumps(cache))


def _is_weekend(d):
    return d.weekday() >= 5  # Saturday=5, Sunday=6


def _cache_key_for_date(date_obj):
    return "weekend" if _is_weekend(date_obj) else "weekday"


def log_event(event):
    """Log event to local JSONL (fallback) and S3 (primary)."""
    event_id = str(_uuid.uuid4())
    event = {"event_id": event_id, "timestamp": datetime.now(timezone.utc).isoformat(), **event}
    # Local fallback
    RUNTIME_DIR.mkdir(exist_ok=True)
    with TRAFFIC_LOG.open("a") as f:
        f.write(json.dumps(event) + "\n")
    # S3 primary
    if EVENTS_S3_BUCKET:
        global _s3_client
        if _s3_client is None:
            _s3_client = boto3.client("s3")
        now = datetime.now(timezone.utc)
        key = f"{EVENTS_S3_PREFIX}{now:%Y/%m/%d}/{event_id}.json"
        try:
            _s3_client.put_object(
                Bucket=EVENTS_S3_BUCKET,
                Key=key,
                Body=json.dumps(event).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as exc:
            print(f"[warn] S3 event log failed: {exc}")
    return event


def make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "mobile": False}
    )


def request_with_retry(scraper, method, url, *, attempts=3, **kwargs):
    """Retry transient ZenPlanner outages before surfacing failure to the UI."""
    last_response = None
    for attempt in range(1, attempts + 1):
        response = getattr(scraper, method)(url, **kwargs)
        text = response.text or ""
        unavailable = (
            response.status_code in (502, 503, 504)
            or "service unavailable" in text.lower()
        )
        if not unavailable or attempt == attempts:
            return response
        last_response = response
        time.sleep(0.75 * attempt)
    return last_response


def normalize_time_label(value):
    raw = value.strip().upper().replace(" ", "")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?([AP]M)", raw)
    if match:
        hour, minute, suffix = match.groups()
        return f"{int(hour)}:{minute or '00'} {suffix}"

    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if match:
        hour, minute = map(int, match.groups())
        suffix = "AM" if hour < 12 else "PM"
        hour = hour % 12 or 12
        return f"{hour}:{minute:02d} {suffix}"

    return value.strip()


def login(scraper, username, password):
    request_with_retry(scraper, "get", f"{BASE_URL}/login.cfm?message=NONE&view=cookies&logout=false")
    login_url = f"{BASE_URL}/login.cfm?view=login"
    response = request_with_retry(scraper, "get", login_url)
    form = BeautifulSoup(response.text, "html.parser").find("form")
    if not form:
        return False, "Login form not found."

    payload = {
        inp["name"]: inp.get("value", "")
        for inp in form.find_all("input")
        if inp.get("name")
    }
    payload["username"] = username
    payload["password"] = password

    action = form.get("action", "/login.cfm")
    post_url = action if action.startswith("http") else BASE_URL + action
    scraper.headers.update({
        "Origin": BASE_URL,
        "Referer": login_url,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    response = request_with_retry(scraper, "post", post_url, data=payload, allow_redirects=False)
    location = response.headers.get("Location", "")
    if response.status_code in (301, 302, 303, 307) and "login" not in location.lower():
        follow_url = location if location.startswith("http") else BASE_URL + location
        scraper.get(follow_url, allow_redirects=True)
        return True, "Login successful."

    return False, "Login failed. Check email/password."


def get_person_id(scraper):
    response = scraper.get(f"{BASE_URL}/calendar.cfm")
    match = re.search(
        r"calendarType=PERSON(?::|%3A)([A-F0-9\-]+)", response.text, re.IGNORECASE
    )
    return match.group(1) if match else None


def get_appointment_id(scraper, person_id, book_date, time_str):
    normalized_time = normalize_time_label(time_str)
    urls = [
        f"{BASE_URL}/calendar.cfm?calendarType=PERSON:{person_id}&date={book_date}&view=LIST",
        f"{BASE_URL}/calendar.cfm?date={book_date}&view=LIST",
    ]
    available = []

    for url in urls:
        response = scraper.get(url)
        soup = BeautifulSoup(response.text, "html.parser")

        for row in soup.select("tr.item"):
            label = row.select_one("td.label div")
            if not label:
                continue

            row_time = label.get_text(strip=True)
            cells = row.select("td.items")
            details = " | ".join(c.get_text(strip=True) for c in cells if c.get_text(strip=True))
            if "Spots for Members" not in details:
                continue
            entry = f"{row_time}: {details}"
            if entry not in available:
                available.append(entry)

            if normalize_time_label(row_time) == normalized_time:
                match = re.search(r"appointmentId=([A-F0-9\-]+)", row.get("onclick", ""), re.IGNORECASE)
                if match:
                    return match.group(1), details, available

    return None, None, available


def discover_membership_id_for_appointment(scraper, person_id, appointment_id):
    """Fetch MembershipId from the enrollment page for a specific appointment."""
    enrollment_url = f"{BASE_URL}/enrollment.cfm?appointmentId={appointment_id}&personId={person_id}"
    enrollment = scraper.get(enrollment_url)
    enrollment_text = enrollment.text

    # Strategy 1: Try select#eligibleMemberships (most reliable)
    soup_enroll = BeautifulSoup(enrollment_text, "html.parser")
    membership_select = soup_enroll.find("select", id="eligibleMemberships") or soup_enroll.find("select", attrs={"name": "eligibleMemberships"})
    if membership_select:
        option = membership_select.find("option", value=True)
        if option and option.get("value"):
            return option["value"].strip()

    # Strategy 2: Regex search for MembershipId in the HTML
    membership = re.search(r"MembershipId=([A-F0-9\-]+)", enrollment_text, re.IGNORECASE)
    if membership:
        return membership.group(1)

    return None


def discover_membership_id_broad(scraper, person_id, start_date=None, target_date=None):
    """Search within the 7-day reservation window to find a MembershipId.

    The web UI only reveals MembershipIds on enrollment pages for slots within
    the reservation window (typically 7 days). The backend, however, accepts
    that MembershipId for bookings outside the window too. We search from
    `start_date` (defaults to today) within +/- 7 days to find any slot where
    the enrollment page exposes a MembershipId.

    If `target_date` is provided, we prioritize dates with the same
    weekday/weekend pattern (weekday vs Sat/Sun) since accounts often have
    separate memberships for weekdays and weekends.
    """
    from datetime import timedelta
    if start_date is None:
        start_date = date.today()

    def _is_weekend(d):
        return d.weekday() >= 5  # Saturday=5, Sunday=6

    target_is_weekend = _is_weekend(target_date) if target_date else None

    # Strategy: search dates matching the target type first, then the opposite type.
    # For each date, check only the first Spots-for-Members slot to minimize requests.
    dates_to_check = []
    for offset in range(0, 8):
        for delta in [timedelta(days=offset), timedelta(days=-offset)]:
            if offset == 0 and delta.days < 0:
                continue
            check_date = start_date + delta
            if check_date < date.today():
                continue
            dates_to_check.append(check_date)

    # Sort so matching type comes first
    if target_is_weekend is not None:
        dates_to_check.sort(key=lambda d: (0 if _is_weekend(d) == target_is_weekend else 1, abs((d - start_date).days)))

    for check_date in dates_to_check:
        urls = [
            f"{BASE_URL}/calendar.cfm?calendarType=PERSON:{person_id}&date={check_date:%Y-%m-%d}&view=LIST",
            f"{BASE_URL}/calendar.cfm?date={check_date:%Y-%m-%d}&view=LIST",
        ]
        for url in urls:
            response = scraper.get(url)
            soup = BeautifulSoup(response.text, "html.parser")
            for row in soup.select("tr.item"):
                details = " | ".join(c.get_text(strip=True) for c in row.select("td.items") if c.get_text(strip=True))
                if "Spots for Members" not in details:
                    continue
                appointment = re.search(r"appointmentId=([A-F0-9\-]+)", row.get("onclick", ""), re.IGNORECASE)
                if not appointment:
                    continue
                mid = discover_membership_id_for_appointment(scraper, person_id, appointment.group(1))
                if mid:
                    return mid, appointment.group(1)
            # If we found spots on the PERSON calendar but no MembershipId,
            # don't waste time on the general calendar for this date.
            if "PERSON" in url:
                break

    return None, None


def _parse_reservation_open_date(text):
    """Parse 'Reservations Open 5/28/26 at 9:00 PM' and return a date object."""
    match = re.search(r"Reservations\s+Open\s+(\d{1,2}/\d{1,2}/(?:\d{2}|\d{4}))", text, re.IGNORECASE)
    if not match:
        return None
    date_str = match.group(1)
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


# An in-window appointment that exposed one family member's memberships; the same page
# exposes the others', which keeps the second person's lookup to one request.
_probe_appointment = None


def get_membership_id(scraper, appointment_id, person_id, target_date=None):
    """Find the MembershipId a reservation is charged to.

    Memberships belong to one person and renew monthly, and ZenPlanner's register
    endpoint does not check that the MembershipId belongs to the personId it is
    registering. A reservation made on someone else's (or an expired) membership is
    accepted and later cancelled by the club. So the cache is keyed by person, and a
    fresh read from any enrollment page that exposes the person's eligible
    memberships always wins over it:

    1. The target appointment's enrollment page (exposes it once the window is open).
    2. Any slot inside the 7-day window with the same weekday/weekend pattern.
    3. The per-person cache, only when nothing in the window exposed one.
    """
    global _probe_appointment
    cache = load_membership_cache()
    type_key = _cache_key_for_date(target_date) if target_date else "any"
    person_cache = cache.get(person_id) if isinstance(cache.get(person_id), dict) else {}

    mid = discover_membership_id_for_appointment(scraper, person_id, appointment_id)
    if not mid and _probe_appointment:
        mid = discover_membership_id_for_appointment(scraper, person_id, _probe_appointment)
    if not mid:
        mid, probe = discover_membership_id_broad(scraper, person_id, start_date=date.today(), target_date=target_date)
        if probe:
            _probe_appointment = probe
    if mid:
        if person_cache.get(type_key) != mid:
            cache[person_id] = {**person_cache, type_key: mid}
            save_membership_cache(cache)
        return mid

    return person_cache.get(type_key)


def get_family_members(scraper, appointment_id, person_id):
    url = f"{BASE_URL}/enrollment.cfm?appointmentId={appointment_id}&personId={person_id}"
    response = scraper.get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    members = {}
    family_select = soup.find("select", id="familyMembers") or soup.find("select", attrs={"name": "familyMembers"})
    if not family_select:
        return {"Primary account": person_id}
    for option in family_select.find_all("option"):
        pid = option.get("value", "").strip()
        name = option.get_text(strip=True)
        if pid and name:
            members[name] = pid
    return members or {"Primary account": person_id}


def reserve(scraper, appointment_id, person_id, membership_id):
    url = (
        f"{BASE_URL}/enrollmentOptions_actions.cfm"
        f"?action=register"
        f"&AppointmentId={appointment_id}"
        f"&personId={person_id}"
        f"&MembershipId={membership_id}"
        f"&nuflo=1"
    )
    response = request_with_retry(scraper, "get", url, allow_redirects=True)
    text = BeautifulSoup(response.text, "html.parser").get_text(separator=" ", strip=True)
    lowered = text.lower()

    if response.status_code in (502, 503, 504) or "service unavailable" in lowered:
        return False, "ZenPlanner is temporarily unavailable after retrying. Please try again in a few minutes."

    if "already have reserved or waitlisted" in lowered or "already reserved" in lowered:
        return True, "already booked"
    if any(term in lowered for term in ["can't reserve", "cannot reserve", "cannot register"]):
        return False, text[:700]
    if any(term in lowered for term in ["full", "error", "unable", "already registered"]):
        return False, text[:700]
    if "is registered for this class" in lowered or "reserved" in lowered:
        return True, text[:700]
    if 200 <= response.status_code < 400:
        return True, "booking request accepted by backend"
    return False, text[:700]


def verify_reservation(scraper, appointment_id, person_id, person_name):
    """Read the person's enrollment page back; only its own confirmation line proves a reservation.

    The register response only proves ZenPlanner accepted the request. Reservations
    that never landed, or that landed on the waitlist, still return a 200 page.
    """
    response = request_with_retry(
        scraper,
        "get",
        f"{BASE_URL}/enrollment.cfm?appointmentId={appointment_id}&personId={person_id}",
        allow_redirects=True,
    )
    text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True).lower()
    if f"{person_name} is registered for this class".lower() in text:
        return "registered"
    if "waitlist" in text and person_name.lower() in text:
        return "waitlisted"
    return "absent"




class BookingError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def book_slot(email, password, book_date, book_time, source="unknown"):
    """Book the selected Spots-for-Members slot for all family members.

    Returns a JSON-serializable dict. Does not persist passwords.
    """
    _start_time = datetime.now(timezone.utc)
    steps = []

    def step(message, status="info", **extra):
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": status,
            "message": message,
        }
        entry.update(extra)
        steps.append(entry)
        print(f"[{status}] {message}")

    email = (email or "").strip().lower()
    password = password or ""
    if not email or not password:
        raise BookingError("validation_error", "Enter both email and password.", steps=steps)
    if not book_date:
        raise BookingError("validation_error", "Choose a booking date.", steps=steps)
    normalized_time = normalize_time_label(book_time or "")
    if normalized_time not in TIMES:
        raise BookingError("validation_error", f"Choose one of: {', '.join(TIMES)}", steps=steps)

    try:
        target_date = datetime.strptime(str(book_date), "%Y-%m-%d").date()
    except ValueError:
        raise BookingError("validation_error", "Date must use YYYY-MM-DD format.", steps=steps)

    event = {
        "email": email,
        "date": f"{target_date:%Y-%m-%d}",
        "time": normalized_time,
        "source": source,
        "status": "started",
    }

    step(f"Starting booking for {email} on {target_date:%Y-%m-%d} at {normalized_time}.")
    step("Creating FeatherFlight session.")
    scraper = make_scraper()

    step("Logging in to ZenPlanner.")
    ok, message = login(scraper, email, password)
    if not ok:
        step(message, "error")
        log_event({**event, "status": "login_failed", "message": message})
        raise BookingError("login_failed", message, steps=steps)
    step("Login successful.", "success")

    step("Finding primary account personId.")
    person_id = get_person_id(scraper)
    if not person_id:
        step("Could not find personId after login.", "error")
        log_event({**event, "status": "person_id_failed"})
        raise BookingError("person_id_failed", "Could not find personId after login.", steps=steps)
    step("Found account calendar.", "success")

    step(f"Searching calendar for Spots for Members at {normalized_time}.")
    appointment_id, slot_details, available = get_appointment_id(
        scraper, person_id, f"{target_date:%Y-%m-%d}", normalized_time
    )
    if not appointment_id:
        step(f"No matching Spots for Members slot found for {normalized_time}.", "error")
        if available:
            step(f"Found {len(available)} other available Spots for Members row(s).")
        log_event({**event, "status": "slot_not_found", "available_slots": available})
        raise BookingError(
            "slot_not_found",
            f"No Spots for Members slot found for {normalized_time} on {target_date:%Y-%m-%d}.",
            available_slots=available,
            steps=steps,
        )
    step(f"Found slot: {slot_details}", "success", appointment_id=appointment_id)

    step("Loading family members for this account.")
    family = get_family_members(scraper, appointment_id, person_id)
    step(f"Booking target(s): {', '.join(family.keys())}.", "success", family_count=len(family))

    results = []
    for name, pid in family.items():
        step(f"{name}: discovering membership ID.")
        membership_id = get_membership_id(scraper, appointment_id, pid, target_date=target_date)

        if not membership_id:
            msg = "Could not discover MembershipId for this person."
            step(f"{name}: {msg}", "error")
            results.append({
                "name": name,
                "success": False,
                "message": msg,
            })
            continue

        step(f"{name}: membership found, submitting reservation.")
        accepted, result = reserve(scraper, appointment_id, pid, membership_id)
        state = verify_reservation(scraper, appointment_id, pid, name)
        success = state == "registered"
        if success:
            message = "booked"
        elif state == "waitlisted":
            message = "waitlisted only, no spot reserved"
        elif accepted:
            message = f"request accepted but the reservation did not appear: {result[:400]}"
        else:
            message = result[:500]
        results.append({
            "name": name,
            "success": success,
            "message": message,
            "verification": state,
        })
        if success:
            step(f"{name}: booked and verified on the enrollment page.", "success")
        else:
            step(f"{name}: booking failed: {message[:220]}", "error")

    success_count = sum(1 for r in results if r["success"])
    failure_count = len(results) - success_count
    all_ok = bool(results) and failure_count == 0
    step(
        f"Finished: {success_count}/{len(results)} target(s) booked.",
        "success" if all_ok else "warning",
    )
    response = {
        "status": "success" if all_ok else "partial_or_failed",
        "message": "Booking completed." if all_ok else "Booking completed with errors.",
        "date": f"{target_date:%Y-%m-%d}",
        "time": normalized_time,
        "slot_details": slot_details,
        "appointment_id": appointment_id,
        "family_count": len(family),
        "success_count": success_count,
        "failure_count": failure_count,
        "results": results,
        "steps": steps,
    }
    duration_ms = int((datetime.now(timezone.utc) - _start_time).total_seconds() * 1000)
    log_event({**event, **response, "duration_ms": duration_ms})
    return response


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": os.environ.get("ALLOW_ORIGIN", "*"),
        "Access-Control-Allow-Headers": "content-type",
        "Access-Control-Allow-Methods": "OPTIONS,POST,GET",
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
    }


def _json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": _cors_headers(),
        "body": json.dumps(body),
    }


def lambda_handler(event, context):
    method = (event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "GET").upper()
    path = event.get("rawPath") or event.get("path") or "/"

    if method == "OPTIONS":
        return _json_response(204, {})
    if method == "GET" and path.endswith("/health"):
        return _json_response(200, {"ok": True, "service": "featherflight-booking"})
    if method != "POST":
        return _json_response(405, {"ok": False, "message": "Use POST /book."})

    try:
        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        payload = json.loads(raw_body)
    except Exception:
        return _json_response(400, {"ok": False, "status": "validation_error", "message": "Invalid JSON body."})

    # Detect source from custom header or query param (frontend sends X-Booking-Source)
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    source = headers.get("x-booking-source", payload.get("source", "web_ui"))

    try:
        result = book_slot(
            payload.get("email"),
            payload.get("password"),
            payload.get("date"),
            payload.get("time"),
            source=source,
        )
        return _json_response(200, {"ok": result["status"] == "success", **result})
    except BookingError as exc:
        code = 400 if exc.status == "validation_error" else 200
        return _json_response(code, {"ok": False, "status": exc.status, "message": exc.message, **exc.extra})
    except Exception as exc:
        # Do not include credentials or stack traces in the browser response.
        print(f"Unhandled error: {type(exc).__name__}: {exc}")
        return _json_response(500, {"ok": False, "status": "server_error", "message": "Unexpected server error. Check Lambda logs."})
