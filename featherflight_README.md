# FeatherFlight Badminton — Auto-Booking Script

Automatically books drop-in slots at [FeatherFlight Badminton Club](https://featherflightbadminton.sites.zenplanner.com) on ZenPlanner.

## Setup

**Python 3.9+ required.**

Install dependencies:
```bash
pip install -r featherflight_web_requirements.txt
```

The CLI prompts for ZenPlanner credentials by default. You can also set `FEATHERFLIGHT_USERNAME` and `FEATHERFLIGHT_PASSWORD` environment variables.

## Usage

```bash
# Book for a single person (primary account holder)
python featherflight_book.py --date 2026-05-30 --time "8:00 PM"

# Book for all family members on the account
python featherflight_book.py --date 2026-05-30 --time "8:00 PM" --person all

# Book for specific family members
python featherflight_book.py --date 2026-05-30 --time "8:00 PM" --person "Nam Nguyen,Dan Nguyen"
```

The script will ask for confirmation before booking.

## Arguments

| Argument | Description | Example |
|---|---|---|
| `--date` | Date to book (YYYY-MM-DD) | `2026-05-30` |
| `--time` | Time slot label exactly as shown on calendar | `"8:00 PM"` |
| `--person` | Name(s), comma-separated, or `all` | `all` |

If `--date` or `--time` are omitted, the script will prompt interactively.

The time parser accepts labels like `9:00 PM`, `9pm`, `9 PM`, and `21:00`.

## Mini Web App

Run the public booking UI locally:
```bash
streamlit run featherflight_mini.py --server.address 127.0.0.1 --server.port 8501
```

Expose it with Cloudflare Tunnel:
```bash
cloudflared tunnel --url http://127.0.0.1:8501
```

The mini app asks each user for email, password, date, and a hardcoded time choice from `4:00 PM` through `9:00 PM`, then books the selected `Spots for Members` slot for all family members on that account.

Run the local-only traffic dashboard:
```bash
streamlit run featherflight_dashboard.py --server.address 127.0.0.1 --server.port 8502
```

## Cookie Persistence

After a successful CLI login, the script saves per-account session cookies to `.runtime/featherflight_cookies_<email>.json` and reuses them on subsequent runs — no login required until the server session expires (typically days to weeks).

## Booking Rules

- **Slots open 1 week in advance** (e.g., Friday slots open the previous Friday at 8:00 PM)
- **Cancellation deadline**: 15 minutes before the class
- **Capacity**: 12 spots per slot

## How It Works

Everything is discovered dynamically at runtime — no hardcoded IDs:

1. **personId** — extracted from the Calendar nav link (`calendarType=PERSON:<uuid>`)
2. **MembershipId** — found by scanning the next 14 days of enrollment pages for the first slot that exposes it, then reused for all bookings in the run
3. **appointmentId** — found from the calendar list view for the requested date/time; only `Spots for Members` rows are used, never `Court Bookings`
4. **Family members** — found from `select#familyMembers` on the enrollment page

ZenPlanner uses ColdFusion endpoints, not a REST API. The actual booking call is a GET to:
```
/enrollmentOptions_actions.cfm?action=register&AppointmentId=...&personId=...&MembershipId=...&nuflo=1
```

Cancellation requires an `AttendanceId` (per-reservation), which is found in the Cancel link on the enrollment page:
```
/enrollmentOptions_actions.cfm?AppointmentId=...&AttendanceId=...&action=cancel
```

## Reservation verification

Every CLI booking now reads the person-specific enrollment page back and reports success only when it sees that exact person registered. Set `--verification daxko` after FeatherFlight provides a Daxko Partner API client ID, secret, and scope; this uses the official attendance endpoint for read-only confirmation. `--verification auto` uses Daxko when configured and otherwise falls back to the member portal. The integration does not cancel reservations.
