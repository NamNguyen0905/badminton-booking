# FeatherFlight Booking Script — Agent Context

This directory contains `featherflight_book.py`, a Python script that auto-books drop-in badminton slots at FeatherFlight Badminton Club (ZenPlanner platform).

## Key Technical Facts

### Platform
- ZenPlanner's member site is ColdFusion-based. The separate Daxko Partner API is available only with club-approved OAuth credentials.
- Cloudflare sits in front — use `cloudscraper` (not `requests`) to bypass JS challenges
- The Daxko Partner API (`api.partners.daxko.com`) requires club-approved OAuth credentials. The read-only verifier uses it only when `DAXKO_CLIENT_ID`, `DAXKO_CLIENT_SECRET`, and `DAXKO_SCOPE` are supplied; never use it to cancel a reservation.

### Authentication
- Login via POST to `/login.cfm` with form fields `username` and `password`
- **Critical**: use `allow_redirects=False` on the POST, then check the `Location` header — success redirects away from `/login.cfm`; failure redirects back to it
- Must set `Origin` and `Referer` headers on the login POST or the server returns a failure redirect
- Session cookies: ColdFusion (`CFID`, `CFTOKEN`) + Cloudflare (`cf_clearance`, `__cf_bm`)
- CLI cookies are persisted per account under `.runtime/featherflight_cookies_<email>.json` and reused across runs
- `session_valid()` must look for the `login.cfm?logout=true` link on `calendar.cfm`. That page renders for anonymous visitors, so "no login redirect" is not proof of a session. The register endpoint accepts anonymous calls, but the per-person readback needs a real login

### ID Discovery (all dynamic, nothing hardcoded)
| ID | Where to find it |
|---|---|
| `personId` | Calendar nav link: `calendarType=PERSON:<uuid>` |
| `MembershipId` | Reserve button href on any open enrollment page: `MembershipId=<uuid>` |
| `appointmentId` | Calendar list view row `onclick`: `enrollment.cfm?appointmentId=<uuid>` |
| `AttendanceId` | Cancel link on enrollment page (only present when already registered) |
| Family members | `select#familyMembers` on enrollment page; do not parse every `<option>` because `eligibleMemberships` also contains options |

### Booking
- Endpoint: GET `/enrollmentOptions_actions.cfm?action=register&AppointmentId=...&personId=...&MembershipId=...&nuflo=1`
- The Reserve button is **not always visible** in a cloudscraper session (weekday evening slots show "Reservations Open [date]" without a button) — the endpoint still works if called directly with valid IDs
- Mini app discovers MembershipId per person from the selected slot's enrollment page first, then falls back to scanning nearby `Spots for Members` slots
- Slot lookup must ignore `Court Bookings` entirely; only rows whose details contain `Spots for Members` should produce appointmentIds

### Cancellation
- Endpoint: GET `/enrollmentOptions_actions.cfm?AppointmentId=...&AttendanceId=...&action=cancel`
- `AttendanceId` is per-reservation — fetch the enrollment page and parse the Cancel link's `href`

### Calendar
- List view URL: `/calendar.cfm?calendarType=PERSON:<personId>&date=YYYY-MM-DD&view=LIST`
- Each row is `tr.item`; time label is in `td.label div`; appointmentId is in the row's `onclick` attribute
- Slots open **exactly 1 week in advance** (e.g., Friday 8PM slots open the prior Friday at 8PM)

## Generalization Notes
- Do not hardcode person IDs, membership IDs, or credentials. They differ by account and can change.
- The mini web app accepts credentials at runtime and does not persist passwords.
- Traffic logging records email, date/time, status, and counts in `.runtime/traffic.jsonl`; it does not log passwords.

## Running the Script
```bash
# From the venv used during development:
cd /var/folders/tv/y3p4mt3x44q3t1v341jvn7k40000gn/T/opencode
venv/bin/python3 /Users/nhtnm/badminton-booking/featherflight_book.py --date YYYY-MM-DD --time "H:MM PM" --person all

# Or with any Python that has dependencies installed:
python3 featherflight_book.py --date 2026-05-30 --time "8:00 PM" --person all
```

## Dependencies
```
cloudscraper
beautifulsoup4
streamlit
pandas
```
