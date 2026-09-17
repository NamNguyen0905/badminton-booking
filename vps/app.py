"""FeatherFlight booking web service: the same engine the weekly cron runs, behind a small HTTP API.

POST /book        {email, password, date, time} -> booking result with per-person verification
GET  /metrics     ?password=&range=30d|all&raw=1 -> dashboard payload
GET  /health      liveness
GET  /            booking page, GET /dashboard.html metrics page
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import date as dt_date
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

import featherflight_book as ff
import metrics

TIMES = ["4:00 PM", "5:00 PM", "6:00 PM", "7:00 PM", "8:00 PM", "9:00 PM"]
EVENTS_FILE = Path(os.environ.get("EVENTS_FILE", "/data/events.jsonl"))
METRICS_PASSWORD = os.environ.get("METRICS_PASSWORD", "")
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Web sessions are one request long and belong to whoever typed the password.
# Nothing about them is written to disk: no cookies, no per-account cache.
ff.save_cookies = lambda scraper: None
ff.save_cache = lambda data: None

_events_lock = threading.Lock()
app = FastAPI(docs_url=None, redoc_url=None)


class BookingError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def log_event(event):
    event = {"event_id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(), **event}
    with _events_lock:
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_FILE.open("a") as handle:
            handle.write(json.dumps(event) + "\n")


def book_slot(email, password, book_date, book_time, source="unknown"):
    started = datetime.now(timezone.utc)
    steps = []

    def step(message, status="info", **extra):
        steps.append({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": status, "message": message, **extra})
        print(f"[{status}] {message}", flush=True)

    email = (email or "").strip().lower()
    password = password or ""
    if not email or not password:
        raise BookingError("validation_error", "Enter both email and password.", steps=steps)
    if not book_date:
        raise BookingError("validation_error", "Choose a booking date.", steps=steps)
    normalized_time = ff.normalize_time_label(book_time or "")
    if normalized_time not in TIMES:
        raise BookingError("validation_error", f"Choose one of: {', '.join(TIMES)}", steps=steps)
    try:
        target_date = datetime.strptime(str(book_date), "%Y-%m-%d").date()
    except ValueError:
        raise BookingError("validation_error", "Date must use YYYY-MM-DD format.", steps=steps)

    event = {"email": email, "date": target_date.isoformat(), "time": normalized_time, "source": source, "status": "started"}
    step(f"Starting booking for {email} on {target_date:%Y-%m-%d} at {normalized_time}.")

    step("Logging in to ZenPlanner.")
    scraper = ff.make_scraper()
    if not ff.login(scraper, email, password):
        message = "Login failed. Check the email and password."
        step(message, "error")
        log_event({**event, "status": "login_failed", "message": message})
        raise BookingError("login_failed", message, steps=steps)
    step("Login successful.", "success")

    person_id = ff.get_person_id(scraper)
    if not person_id:
        step("Could not find personId after login.", "error")
        log_event({**event, "status": "person_id_failed"})
        raise BookingError("person_id_failed", "Could not find personId after login.", steps=steps)
    step("Found account calendar.", "success")

    step(f"Searching calendar for Spots for Members at {normalized_time}.")
    appointment_id, slot_details, available = ff.find_slot(scraper, person_id, target_date.isoformat(), normalized_time)
    if not appointment_id:
        step(f"No matching Spots for Members slot found for {normalized_time}.", "error")
        log_event({**event, "status": "slot_not_found", "available_slots": available})
        raise BookingError(
            "slot_not_found",
            f"No Spots for Members slot found for {normalized_time} on {target_date:%Y-%m-%d}.",
            available_slots=available,
            steps=steps,
        )
    step(f"Found slot: {slot_details}", "success", appointment_id=appointment_id)

    family = ff.get_family_members(scraper, appointment_id, person_id)
    step(f"Booking target(s): {', '.join(family.keys())}.", "success", family_count=len(family))

    results = []
    for name, pid in family.items():
        step(f"{name}: discovering membership ID.")
        membership_id = ff.discover_membership_id(scraper, pid, appointment_id, target_date=target_date)
        if not membership_id:
            message = "Could not find an eligible membership for this person."
            step(f"{name}: {message}", "error")
            results.append({"name": name, "success": False, "message": message, "verification": "unavailable"})
            continue

        step(f"{name}: membership found, submitting reservation.")
        request_result = ff.reserve(scraper, appointment_id, pid, membership_id)
        verification = ff.verify_portal_reservation(scraper, appointment_id, pid, name)
        state = verification.get("state", "unavailable")
        success = state == "registered"
        if success:
            message = "booked"
        elif state == "waitlisted":
            message = "waitlisted only, no spot reserved"
        elif request_result.get("rejection_found"):
            message = "ZenPlanner rejected the reservation (full or not eligible)."
        elif state == "unavailable":
            message = f"Could not read the reservation back: {verification.get('detail', 'unknown')}"
        else:
            message = "Request went through but the reservation did not appear on the enrollment page."
        results.append({"name": name, "success": success, "message": message, "verification": state})
        step(f"{name}: {'booked and verified on the enrollment page.' if success else 'booking failed: ' + message}", "success" if success else "error")

    success_count = sum(1 for r in results if r["success"])
    failure_count = len(results) - success_count
    all_ok = bool(results) and failure_count == 0
    step(f"Finished: {success_count}/{len(results)} target(s) booked.", "success" if all_ok else "warning")
    response = {
        "status": "success" if all_ok else "partial_or_failed",
        "message": "Booking completed." if all_ok else "Booking completed with errors.",
        "date": target_date.isoformat(),
        "time": normalized_time,
        "slot_details": slot_details,
        "appointment_id": appointment_id,
        "family_count": len(family),
        "success_count": success_count,
        "failure_count": failure_count,
        "results": results,
        "steps": steps,
    }
    log_event({**event, **response, "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000)})
    return response


@app.get("/health")
@app.get("/healthz")
def health():
    return {"ok": True, "service": "featherflight-booking"}


@app.post("/book")
async def book(request: Request, x_booking_source: str | None = Header(default=None)):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "status": "validation_error", "message": "Invalid JSON body."}, status_code=400)
    source = x_booking_source or payload.get("source") or "web_ui"
    try:
        # The engine is blocking HTTP; keep the event loop free for other requests.
        import anyio

        result = await anyio.to_thread.run_sync(
            lambda: book_slot(payload.get("email"), payload.get("password"), payload.get("date"), payload.get("time"), source)
        )
        return JSONResponse({"ok": result["status"] == "success", **result}, headers={"Cache-Control": "no-store"})
    except BookingError as exc:
        code = 400 if exc.status == "validation_error" else 200
        return JSONResponse({"ok": False, "status": exc.status, "message": exc.message, **exc.extra}, status_code=code)
    except Exception as exc:
        # Never echo credentials or stack traces to the browser.
        print(f"Unhandled error: {type(exc).__name__}: {exc}", flush=True)
        return JSONResponse({"ok": False, "status": "server_error", "message": "Unexpected server error. Check the service logs."}, status_code=500)


@app.get("/metrics")
def metrics_endpoint(password: str = "", range: str = "30d", raw: str = ""):
    if not METRICS_PASSWORD:
        return JSONResponse({"ok": False, "message": "METRICS_PASSWORD not configured."}, status_code=500)
    if password != METRICS_PASSWORD:
        return JSONResponse({"ok": False, "message": "Invalid password."}, status_code=401)
    events = metrics.load_events(EVENTS_FILE, *metrics.parse_range(range))
    if raw == "1":
        events = sorted(events, key=lambda e: e.get("timestamp", ""), reverse=True)[:500]
        return JSONResponse({"ok": True, "count": len(events), "events": events}, headers={"Cache-Control": "no-store"})
    return JSONResponse({"ok": True, "range": range, **metrics.aggregate(events)}, headers={"Cache-Control": "no-store"})


def _page(name, replacements):
    html = (STATIC_DIR / name).read_text()
    for old, new in replacements.items():
        html = html.replace(old, new)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/")
@app.get("/index.html")
def index():
    return _page("index.html", {"https://kvryaghlgk.execute-api.us-west-2.amazonaws.com/book": "/book"})


@app.get("/dashboard.html")
def dashboard():
    return _page("dashboard.html", {"__METRICS_URL__": "/metrics"})
