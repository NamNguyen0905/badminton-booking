#!/usr/bin/env python3
"""Minimal Streamlit UI for booking FeatherFlight slots.

Users enter ZenPlanner credentials, date, and time. The app logs in, discovers
account IDs, and books the requested slot for all family members on the account.
"""

import re
import json
from datetime import date
from datetime import datetime, timezone
from pathlib import Path

import cloudscraper
import streamlit as st
from bs4 import BeautifulSoup


BASE_URL = "https://featherflightbadminton.sites.zenplanner.com"
TIMES = ["4:00 PM", "5:00 PM", "6:00 PM", "7:00 PM", "8:00 PM", "9:00 PM"]
RUNTIME_DIR = Path(__file__).resolve().parent / ".runtime"
TRAFFIC_LOG = RUNTIME_DIR / "traffic.jsonl"
MEMBERSHIP_CACHE_FILE = RUNTIME_DIR / "membership_cache.json"


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
    RUNTIME_DIR.mkdir(exist_ok=True)
    event = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
    with TRAFFIC_LOG.open("a") as f:
        f.write(json.dumps(event) + "\n")


def make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False}
    )


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
    scraper.get(f"{BASE_URL}/login.cfm?message=NONE&view=cookies&logout=false")
    login_url = f"{BASE_URL}/login.cfm?view=login"
    response = scraper.get(login_url)
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

    response = scraper.post(post_url, data=payload, allow_redirects=False)
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
                    return mid
            # If we found spots on the PERSON calendar but no MembershipId,
            # don't waste time on the general calendar for this date.
            if "PERSON" in url:
                break

    return None


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


def get_membership_id(scraper, appointment_id, person_id, email="", target_date=None):
    """Find MembershipId for a person: cache first, then per-appointment, then broad search.

    The ZenPlanner web UI only shows MembershipIds on enrollment pages for slots
    within the reservation window (typically 7 days). The backend, however,
    accepts a valid MembershipId for any date. We therefore:

    1. Check the cache first (MembershipIds are reusable across dates for the same account).
    2. Try the specific appointment's enrollment page (works if within the window).
    3. Search the 7-day window from today to find any slot that exposes a MembershipId,
       preferring dates with the same weekday/weekend pattern as the target.
    """
    cache = load_membership_cache()
    email_key = email.strip().lower()
    type_key = _cache_key_for_date(target_date) if target_date else None

    # 1. Check cache for the specific type (weekday/weekend)
    if email_key and email_key in cache:
        if isinstance(cache[email_key], dict) and type_key and type_key in cache[email_key]:
            return cache[email_key][type_key]
        # Backwards compat: old flat cache format
        if isinstance(cache[email_key], str):
            old_mid = cache[email_key]
            # Migrate to new format by discovering the other type if possible
            if type_key:
                cache[email_key] = {type_key: old_mid}
                save_membership_cache(cache)
            return old_mid

    # Strategy 1: Try the specific appointment's enrollment page
    mid = discover_membership_id_for_appointment(scraper, person_id, appointment_id)
    if mid:
        if email_key and type_key:
            cache.setdefault(email_key, {})[type_key] = mid
            save_membership_cache(cache)
        return mid

    # Strategy 2: Search the 7-day window from today for any valid MembershipId
    mid = discover_membership_id_broad(scraper, person_id, start_date=date.today(), target_date=target_date)
    if mid:
        if email_key and type_key:
            cache.setdefault(email_key, {})[type_key] = mid
            save_membership_cache(cache)
        return mid

    return None


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
    response = scraper.get(url, allow_redirects=True)
    text = BeautifulSoup(response.text, "html.parser").get_text(separator=" ", strip=True)
    lowered = text.lower()

    if any(term in lowered for term in ["can't reserve", "cannot reserve", "cannot register"]):
        return False, text[:700]
    if "is registered for this class" in lowered or "reserved" in lowered:
        return True, text[:700]
    return False, text[:700]


st.set_page_config(page_title="FeatherFlight Booking")
st.title("FeatherFlight Booking")
st.caption("Enter ZenPlanner credentials, choose a date/time, and book for all family members.")

email = st.text_input("Email")
password = st.text_input("Password", type="password")
book_date = st.date_input("Date", value=date.today())
book_time = st.selectbox("Time", TIMES, index=4)

if st.button("Book", type="primary"):
    if not email or not password:
        st.error("Enter both email and password.")
        st.stop()

    event = {
        "email": email.strip().lower(),
        "date": f"{book_date:%Y-%m-%d}",
        "time": book_time,
        "status": "started",
    }
    scraper = make_scraper()

    with st.status("Booking...", expanded=True) as status:
        ok, message = login(scraper, email, password)
        if not ok:
            log_event({**event, "status": "login_failed", "message": message})
            st.error(message)
            status.update(label="Failed", state="error")
            st.stop()
        st.write("Login successful.")

        person_id = get_person_id(scraper)
        if not person_id:
            log_event({**event, "status": "person_id_failed"})
            st.error("Could not find personId after login.")
            status.update(label="Failed", state="error")
            st.stop()

        appointment_id, slot_details, available = get_appointment_id(
            scraper, person_id, f"{book_date:%Y-%m-%d}", book_time
        )
        if not appointment_id:
            log_event({**event, "status": "slot_not_found", "available_slots": available})
            st.error(f"No slot found for {book_time} on {book_date:%Y-%m-%d}.")
            if available:
                st.write("Available slots:")
                st.code("\n".join(available))
            else:
                st.info("Your account's calendar shows no slots for this date. This can happen if:\n"
                       "• You have a Weekend-only membership (valid Fri-Sun)\n"
                       "• The slot hasn't been released yet (check 'Reservations Open' date)\n"
                       "• The slot is already full or not offered on this day")
            status.update(label="Failed", state="error")
            st.stop()
        st.write(f"Found slot: {book_time} ({slot_details})")

        family = get_family_members(scraper, appointment_id, person_id)
        st.write(f"Booking for: {', '.join(family.keys())}")

        all_ok = True
        results = []
        for name, pid in family.items():
            membership_id = get_membership_id(scraper, appointment_id, pid, email, target_date=book_date)
            if not membership_id:
                # Even if we couldn't find a MembershipId on the enrollment page,
                # try a broad search and then attempt booking anyway. The API
                # often accepts MembershipIds that the web UI hides.
                membership_id = discover_membership_id_broad(scraper, pid, start_date=date.today(), target_date=book_date)
                if membership_id:
                    st.write(f"{name}: MembershipId discovered from nearby dates, attempting booking...")

            if not membership_id:
                all_ok = False
                results.append({"name": name, "success": False})
                st.error(f"{name}: failed")
                st.caption("Could not discover MembershipId for this person. They may not have an active membership that covers this slot type.")
                continue

            success, result = reserve(scraper, appointment_id, pid, membership_id)
            results.append({"name": name, "success": success})
            if success:
                st.success(f"{name}: booked")
            else:
                all_ok = False
                st.error(f"{name}: failed")
                st.caption(result)

        log_event({
            **event,
            "status": "success" if all_ok else "partial_or_failed",
            "appointment_id": appointment_id,
            "family_count": len(family),
            "success_count": sum(1 for r in results if r["success"]),
            "failure_count": sum(1 for r in results if not r["success"]),
            "results": results,
        })

        status.update(label="Done" if all_ok else "Completed with errors", state="complete" if all_ok else "error")
