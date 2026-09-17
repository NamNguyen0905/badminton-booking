#!/usr/bin/env python3
"""
FeatherFlight Badminton — auto-book a drop-in slot.

Usage:
    python featherflight_book.py

Set USERNAME, PASSWORD, BOOK_DATE, and BOOK_TIME before running.
Everything else (personId, membershipId, appointmentId) is discovered automatically.
"""

import re
import sys
import json
import os
import argparse
import getpass
import cloudscraper
from bs4 import BeautifulSoup
from datetime import date as dt_date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from daxko_reservations import verify_reservation as verify_daxko_reservation

BASE_URL = "https://featherflightbadminton.sites.zenplanner.com"
RUNTIME_DIR = Path(__file__).resolve().parent / ".runtime"
COOKIE_FILE = RUNTIME_DIR / "featherflight_cookies.json"
CACHE_FILE  = RUNTIME_DIR / "featherflight_cache.json"
HTTP_TIMEOUT = (10, 30)
RESULT_PREFIX = "FEATHERFLIGHT_RESULT_JSON="

# --- Auth (optional env vars; otherwise prompted) ---
USERNAME = os.environ.get("FEATHERFLIGHT_USERNAME", "")
PASSWORD = os.environ.get("FEATHERFLIGHT_PASSWORD", "")


def configure_runtime_files(username):
    global COOKIE_FILE, CACHE_FILE
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", username.strip().lower()) or "default"
    COOKIE_FILE = RUNTIME_DIR / f"featherflight_cookies_{safe}.json"
    CACHE_FILE = RUNTIME_DIR / f"featherflight_cache_{safe}.json"


def load_cache():
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def write_private_json(path, payload):
    RUNTIME_DIR.mkdir(mode=0o700, exist_ok=True)
    RUNTIME_DIR.chmod(0o700)
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def save_cache(data):
    existing = load_cache()
    existing.update(data)
    write_private_json(CACHE_FILE, existing)


def save_cookies(scraper):
    cookies = {c.name: c.value for c in scraper.cookies}
    write_private_json(COOKIE_FILE, cookies)


def load_cookies(scraper):
    if not COOKIE_FILE.exists():
        return False
    try:
        cookies = json.loads(COOKIE_FILE.read_text())
        scraper.cookies.update(cookies)
        return True
    except Exception:
        return False


def http_get(scraper, url, **kwargs):
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    return scraper.get(url, **kwargs)


def http_post(scraper, url, **kwargs):
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    return scraper.post(url, **kwargs)


def page_kind(url):
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != urlparse(BASE_URL).netloc:
        return "external"
    page = Path(parsed.path).name.lower()
    if page == "login.cfm":
        return "login"
    if page == "enrollment.cfm":
        return "enrollment"
    if page == "enrollmentoptions_actions.cfm":
        return "registration"
    if page == "calendar.cfm":
        return "calendar"
    return "zenplanner"


def session_valid(scraper):
    # calendar.cfm renders for anonymous visitors too, so the absence of a login
    # redirect proves nothing. Only a logout link proves the cookies still carry a member session.
    r = http_get(scraper, f"{BASE_URL}/calendar.cfm", allow_redirects=True)
    if page_kind(r.url) == "login":
        return False
    soup = BeautifulSoup(r.text, "html.parser")
    return any("logout=true" in a["href"].lower() for a in soup.find_all("a", href=True))


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
    print("[*] Logging in...")
    http_get(scraper, f"{BASE_URL}/login.cfm?message=NONE&view=cookies&logout=false")

    login_url = f"{BASE_URL}/login.cfm?view=login"
    r = http_get(scraper, login_url)
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form")
    if not form:
        print("[-] Login form not found.")
        return False

    payload = {inp["name"]: inp.get("value", "") for inp in form.find_all("input") if inp.get("name")}
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

    r = http_post(scraper, post_url, data=payload, allow_redirects=False)
    location = r.headers.get("Location", "")

    if r.status_code in (301, 302, 303, 307) and "login" not in location.lower():
        print(f"[+] Login successful.")
        follow_url = location if location.startswith("http") else BASE_URL + location
        http_get(scraper, follow_url, allow_redirects=True)
        save_cookies(scraper)
        return True

    dest = (BASE_URL + location) if (location and not location.startswith("http")) else (location or post_url)
    r2 = http_get(scraper, dest)
    soup2 = BeautifulSoup(r2.text, "html.parser")
    error = soup2.find(class_="red") or soup2.find(class_="error")
    msg = error.get_text(strip=True) if error else f"redirected to {location} (status {r.status_code})"
    print(f"[-] Login failed: {msg}")
    return False


def get_person_id(scraper):
    """Extract the primary account holder's personId from the Calendar nav link."""
    r = http_get(scraper, f"{BASE_URL}/calendar.cfm")
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        m = re.search(r"calendarType=PERSON(?::|%3A)([A-F0-9\-]+)", a["href"], re.IGNORECASE)
        if m:
            person_id = m.group(1)
            print(f"[+] Primary personId: {person_id}")
            return person_id
    print("[-] Could not find personId.")
    return None


def discover_membership_id_for_appointment(scraper, person_id, appointment_id):
    """Fetch MembershipId from the enrollment page for a specific appointment."""
    enrollment_url = f"{BASE_URL}/enrollment.cfm?appointmentId={appointment_id}&personId={person_id}"
    enrollment = http_get(scraper, enrollment_url)
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
    """
    Search within the 7-day reservation window to find a MembershipId.
    
    The web UI only reveals MembershipIds on enrollment pages for slots within
    the reservation window (typically 7 days). The backend, however, accepts
    that MembershipId for bookings outside the window too. We search from
    `start_date` (defaults to today) within +/- 7 days to find any slot where
    the enrollment page exposes a MembershipId.
    
    If `target_date` is provided, we prioritize dates with the same
    weekday/weekend pattern since accounts often have separate memberships.
    For each date, we check only the first Spots-for-Members slot to minimize
    HTTP requests.
    """
    if start_date is None:
        start_date = dt_date.today()
    
    def _is_weekend(d):
        return d.weekday() >= 5  # Saturday=5, Sunday=6
    
    target_is_weekend = _is_weekend(target_date) if target_date else None
    
    # Build list of dates to check
    dates_to_check = []
    for offset in range(0, 8):
        for delta in [timedelta(days=offset), timedelta(days=-offset)]:
            if offset == 0 and delta.days < 0:
                continue
            check_date = start_date + delta
            if check_date < dt_date.today():
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
            response = http_get(scraper, url)
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
            # If person-specific calendar had spots but no MembershipId,
            # skip the general calendar for this date to save requests.
            if "PERSON" in url:
                break

    return None, None


# An in-window appointment that exposed one family member's memberships; the same page
# exposes the others', which keeps the second person's lookup to one request.
_probe_appointment = None


def discover_membership_id(scraper, person_id, appointment_id=None, target_date=None):
    """
    Discover MembershipId: first try the specific appointment, then broad search.
    """
    global _probe_appointment
    if appointment_id:
        mid = discover_membership_id_for_appointment(scraper, person_id, appointment_id)
        if mid:
            print(f"[+] MembershipId discovered from appointment enrollment: {mid}")
            return mid
    if _probe_appointment:
        mid = discover_membership_id_for_appointment(scraper, person_id, _probe_appointment)
        if mid:
            print(f"[+] MembershipId discovered from the probe appointment: {mid}")
            return mid

    mid, probe = discover_membership_id_broad(scraper, person_id, start_date=dt_date.today(), target_date=target_date)
    if mid:
        _probe_appointment = probe
        print(f"[+] MembershipId discovered from nearby dates: {mid}")
        return mid

    print("[-] Could not discover MembershipId.")
    return None


def membership_for_person(scraper, person_id, appointment_id, target_date, memberships):
    """Resolve the membership this person's reservation is charged to.

    Memberships are per person and renew monthly, and the register endpoint does not
    check that the MembershipId belongs to the personId: a reservation on someone
    else's or an expired membership is accepted, then cancelled by the club. A fresh
    read from any enrollment page that lists the person's eligible memberships wins;
    the per-person cache is only a fallback for when nothing in the window exposed one.
    """
    mid = discover_membership_id(scraper, person_id, appointment_id, target_date=target_date)
    if mid:
        if memberships.get(person_id) != mid:
            memberships[person_id] = mid
            save_cache({"memberships": memberships})
        return mid
    cached = memberships.get(person_id)
    if cached:
        print(f"[~] Using cached MembershipId for this person; nothing in the window exposed a fresh one.")
    return cached


def get_family_members(scraper, appointment_id, person_id):
    """
    Return a dict of {name: personId} for all family members on the account,
    discovered from the enrollment page's family member selector.
    """
    url = f"{BASE_URL}/enrollment.cfm?appointmentId={appointment_id}&personId={person_id}"
    r = http_get(scraper, url)
    soup = BeautifulSoup(r.text, "html.parser")
    members = {}
    family_select = soup.find("select", id="familyMembers") or soup.find("select", attrs={"name": "familyMembers"})
    if not family_select:
        return {"Primary account": person_id}
    for opt in family_select.find_all("option"):
        pid = opt.get("value", "").strip()
        name = opt.get_text(strip=True)
        if pid and name:
            members[name] = pid
    if members:
        print(f"[+] Family members: {list(members.keys())}")
    return members


def find_slot(scraper, person_id, date, time_str):
    """
    Fetch the calendar for `date` and find the Spots for Members row matching `time_str`.
    `date` is YYYY-MM-DD, `time_str` is e.g. '9:00 PM'.
    Returns (appointment_id, slot_details, available_rows); appointment_id is None when absent.
    """
    urls = [
        f"{BASE_URL}/calendar.cfm?calendarType=PERSON:{person_id}&date={date}&view=LIST",
        f"{BASE_URL}/calendar.cfm?date={date}&view=LIST",
    ]
    normalized_time = normalize_time_label(time_str)
    available = []

    # Each row has an onclick like: enrollment.cfm?appointmentId=<uuid>
    # and a label div with the time text
    for url in urls:
        r = http_get(scraper, url)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("tr.item"):
            label = row.select_one("td.label div")
            if not label:
                continue
            cells = row.select("td.items")
            spots_info = " | ".join(c.get_text(strip=True) for c in cells if c.get_text(strip=True))
            if "Spots for Members" not in spots_info:
                continue
            entry = f"{label.get_text(strip=True):12} {spots_info}"
            if entry not in available:
                available.append(entry)
            if normalize_time_label(label.get_text(strip=True)) == normalized_time:
                onclick = row.get("onclick", "")
                m = re.search(r"appointmentId=([A-F0-9\-]+)", onclick, re.IGNORECASE)
                if m:
                    return m.group(1), spots_info, available

    return None, None, available


def get_appointment_id(scraper, person_id, date, time_str):
    appt_id, spots_info, available = find_slot(scraper, person_id, date, time_str)
    if appt_id:
        print(f"[+] Found {time_str} on {date}: {appt_id} ({spots_info})")
        return appt_id

    # Show available times if not found
    print(f"[-] No slot found for {time_str} on {date}. Available:")
    for entry in available:
        print(f"    {entry}")
    return None


def reserve(scraper, appointment_id, person_id, membership_id):
    """Hit the register endpoint — mirrors clicking the Reserve button."""
    url = (
        f"{BASE_URL}/enrollmentOptions_actions.cfm"
        f"?action=register"
        f"&AppointmentId={appointment_id}"
        f"&personId={person_id}"
        f"&MembershipId={membership_id}"
        f"&nuflo=1"
    )
    print(f"\n--- Booking ---\nGET {url}")
    r = http_get(scraper, url, allow_redirects=True)
    print(f"HTTP {r.status_code} final_url={r.url}")
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    print(text[:1000])
    lowered = text.lower()
    final_page = page_kind(r.url)
    redirected_to_login = final_page == "login" or any(
        "login" in response.headers.get("Location", "").lower() for response in r.history
    )
    rejection_found = any(
        term in lowered
        for term in ["can't reserve", "cannot reserve", "cannot register", "full", "error", "unable", "already registered"]
    )
    result = {
        "status": "ambiguous",
        "http_status": r.status_code,
        "final_page": final_page,
        "rejection_found": rejection_found,
    }

    # This response only proves that ZenPlanner accepted the request. The caller
    # must read the reservation state back before reporting a successful booking.
    if redirected_to_login:
        print("\n[~] Check the output above to confirm status.")
    elif final_page == "external":
        print("\n[~] Check the output above to confirm status.")
    elif 200 <= r.status_code < 400:
        result["status"] = "submitted"
        print("\n[*] Booking request returned. Verifying reservation state.")
    else:
        result["status"] = "failed"
        print("\n[~] Check the output above to confirm status.")
    return result


def verify_portal_reservation(scraper, appointment_id, person_id, person_name):
    """Read the enrollment page for one person without changing reservation state."""
    response = http_get(
        scraper,
        f"{BASE_URL}/enrollment.cfm?appointmentId={appointment_id}&personId={person_id}",
        allow_redirects=True,
    )
    if page_kind(response.url) == "login":
        return {"method": "portal", "state": "unavailable", "detail": "member session expired"}

    text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True).lower()
    expected = f"{person_name} is registered for this class".lower()
    if expected in text:
        return {"method": "portal", "state": "registered"}
    if "waitlist" in text and ("you are" in text or person_name.lower() in text):
        return {"method": "portal", "state": "waitlisted"}
    return {"method": "portal", "state": "absent"}


def verify_reservation(scraper, appointment_id, person_id, person_name, target_date, target_time, method):
    if method in {"auto", "daxko"}:
        daxko = verify_daxko_reservation(person_id, target_date, target_time)
        if daxko["state"] != "unavailable" or method == "daxko":
            return daxko
    return verify_portal_reservation(scraper, appointment_id, person_id, person_name)


def finalize_reservation(result, verification):
    result["verification"] = verification
    if result["status"] == "ambiguous":
        return result
    if verification["state"] == "registered":
        result["status"] = "confirmed"
    elif verification["state"] == "waitlisted":
        result["status"] = "waitlisted"
    elif verification["state"] == "absent":
        result["status"] = "failed"
    else:
        result["status"] = "ambiguous"
    return result


def parse_args():
    parser = argparse.ArgumentParser(description="Book a FeatherFlight Badminton slot.")
    parser.add_argument("--date",        help="Date to book (YYYY-MM-DD), e.g. 2026-05-28")
    parser.add_argument("--time",        help="Time slot, e.g. '9:00 PM'")
    parser.add_argument("--person",      help="Name(s) to book for, comma-separated, or 'all'. Default: primary account holder.")
    parser.add_argument("--clear-cache", action="store_true", help="Discard cached personId/membershipId/family and re-discover.")
    parser.add_argument("--result-json", action="store_true", help="Append one machine-readable result line for automation.")
    parser.add_argument(
        "--verification",
        choices=("auto", "portal", "daxko"),
        default="auto",
        help="Verify each reservation with Daxko when configured, otherwise the member portal.",
    )
    args = parser.parse_args()

    date = args.date or input("Enter date (YYYY-MM-DD): ").strip()
    time_str = args.time or input("Enter time (e.g. '9:00 PM'): ").strip()
    person_filter = args.person or ""

    return date, time_str, person_filter, args.clear_cache, args.result_json, args.verification


def main():
    username = USERNAME or input("ZenPlanner email: ").strip()
    password = PASSWORD or getpass.getpass("ZenPlanner password: ")
    if not username or not password:
        print("Error: email and password are required.")
        sys.exit(1)
    configure_runtime_files(username)

    date, time_str, person_filter, clear_cache, result_json, verification_method = parse_args()

    if clear_cache and CACHE_FILE.exists():
        CACHE_FILE.unlink()
        print("[*] Cache cleared.")

    scraper = make_scraper()

    if load_cookies(scraper) and session_valid(scraper):
        print("[+] Reusing saved session.")
    elif not login(scraper, username, password):
        sys.exit(1)

    cache = load_cache()

    if cache.get("person_id"):
        primary_person_id = cache["person_id"]
        print(f"[+] personId (cached): {primary_person_id}")
    else:
        primary_person_id = get_person_id(scraper)
        if not primary_person_id:
            sys.exit(1)
        save_cache({"person_id": primary_person_id})

    appointment_id = get_appointment_id(scraper, primary_person_id, date, time_str)
    if not appointment_id:
        sys.exit(1)

    if cache.get("family_members"):
        family = cache["family_members"]
        print(f"[+] Family members (cached): {list(family.keys())}")
    else:
        family = get_family_members(scraper, appointment_id, primary_person_id)
        save_cache({"family_members": family})

    if person_filter.lower() == "all":
        targets = family  # {name: personId}
    elif person_filter:
        requested = [n.strip().lower() for n in person_filter.split(",")]
        targets = {
            n: pid for n, pid in family.items()
            if any(n.lower() == r or n.lower().split()[0] == r for r in requested)
        }
        missing = [r for r in requested if not any(n.lower() == r or n.lower().split()[0] == r for n in family)]
        if missing:
            print(f"[-] Unknown person(s): {missing}. Available: {list(family.keys())}")
            sys.exit(1)
    else:
        # Default: primary account holder only
        primary_name = next((n for n, pid in family.items() if pid == primary_person_id), "primary")
        targets = {primary_name: primary_person_id}

    print(f"\n[*] Will book for: {list(targets.keys())}")
    confirm = input(f"Proceed with booking {time_str} on {date}? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    results = []
    target_date = dt_date.fromisoformat(date)
    memberships = cache.get("memberships") if isinstance(cache.get("memberships"), dict) else {}
    for name, person_id in targets.items():
        print(f"\n{'='*50}\nBooking for {name}...")
        membership_id = membership_for_person(scraper, person_id, appointment_id, target_date, memberships)
        if not membership_id:
            print(f"[-] No membership found for {name}; not submitting a reservation.")
            results.append({
                "person": name,
                "status": "failed",
                "http_status": 0,
                "final_page": "none",
                "rejection_found": False,
                "verification": {"method": "none", "state": "unavailable", "detail": "no membership id"},
            })
            continue
        result = reserve(scraper, appointment_id, person_id, membership_id)
        result["person"] = name
        result = finalize_reservation(
            result,
            verify_reservation(
                scraper,
                appointment_id,
                person_id,
                name,
                dt_date.fromisoformat(date),
                normalize_time_label(time_str),
                verification_method,
            ),
        )
        print(f"[+] Verification: {result['verification']['method']} {result['verification']['state']}.")
        results.append(result)

    if result_json:
        payload = {"date": date, "time": time_str, "results": results}
        print(f"{RESULT_PREFIX}{json.dumps(payload, separators=(',', ':'))}")

    if any(result["status"] != "confirmed" for result in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
