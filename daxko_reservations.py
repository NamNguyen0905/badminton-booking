"""Read-only Zen Planner reservation verification through Daxko's Partner API."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


AUTH_URL = "https://api.partners.daxko.com/auth/token"
API_BASE_URL = "https://api.partners.daxko.com/api/v1"
TIMEOUT_SECONDS = 30
TIMEZONE = ZoneInfo("America/Los_Angeles")


def is_configured() -> bool:
    return all(os.environ.get(name) for name in ("DAXKO_CLIENT_ID", "DAXKO_CLIENT_SECRET", "DAXKO_SCOPE"))


def _request(url: str, *, token: str | None = None, payload: dict[str, str] | None = None) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
            return response.status, payload if isinstance(payload, dict) else {}
    except HTTPError as exc:
        return exc.code, {}
    except (URLError, TimeoutError, json.JSONDecodeError):
        return 0, {}


def _access_token() -> str | None:
    status, payload = _request(
        AUTH_URL,
        payload={
            "client_id": os.environ["DAXKO_CLIENT_ID"],
            "client_secret": os.environ["DAXKO_CLIENT_SECRET"],
            "scope": os.environ["DAXKO_SCOPE"],
            "grant_type": "client_credentials",
        },
    )
    token = payload.get("access_token") if status == 200 else None
    return token if isinstance(token, str) and token else None


def verify_reservation(person_id: str, target_date: date, target_time: str) -> dict[str, str]:
    """Return the reservation state for one person and local calendar date.

    Daxko's documented attendance endpoint supports reservations, cancellations,
    and waitlists. Credentials belong to the club's partner integration, never
    to a member account. This function deliberately performs no write operation.
    """
    if not is_configured():
        return {"method": "daxko", "state": "unavailable", "detail": "partner credentials are not configured"}

    token = _access_token()
    if not token:
        return {"method": "daxko", "state": "unavailable", "detail": "partner authentication failed"}

    start = target_date - timedelta(days=1)
    end = target_date + timedelta(days=2)
    base_url = os.environ.get("DAXKO_API_BASE_URL", API_BASE_URL).rstrip("/")
    url = (
        f"{base_url}/classes/people/{person_id}/attendances"
        f"?appointmentBeginDate={start.isoformat()}T00:00:00.000Z"
        f"&appointmentEndDate={end.isoformat()}T00:00:00.000Z"
        "&includeWaitlist=true&includeCancellations=true&pageSize=100"
    )
    status, payload = _request(url, token=token)
    if status != 200:
        return {"method": "daxko", "state": "unavailable", "detail": f"attendance query returned HTTP {status or 'network_error'}"}

    elements = payload.get("elements")
    if not isinstance(elements, list):
        return {"method": "daxko", "state": "unavailable", "detail": "attendance response was incomplete"}

    matching = []
    for attendance in elements:
        if not isinstance(attendance, dict):
            continue
        class_info = attendance.get("class")
        start_date = class_info.get("startDate", "") if isinstance(class_info, dict) else ""
        if not isinstance(start_date, str):
            continue
        try:
            local_start = datetime.fromisoformat(start_date.replace("Z", "+00:00")).astimezone(TIMEZONE)
        except ValueError:
            continue
        local_time = f"{local_start.hour % 12 or 12}:{local_start.minute:02d} {'AM' if local_start.hour < 12 else 'PM'}"
        if local_start.date() == target_date and local_time == target_time:
            matching.append(attendance)

    if any(item.get("isReserved") and not item.get("isCanceled") for item in matching):
        return {"method": "daxko", "state": "registered"}
    if matching and any(item.get("waitlist") for item in matching):
        return {"method": "daxko", "state": "waitlisted"}
    return {"method": "daxko", "state": "absent"}
