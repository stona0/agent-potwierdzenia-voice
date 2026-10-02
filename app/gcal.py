"""Odczyt spotkań z Google Calendar i zapis wyniku potwierdzenia z powrotem do wydarzenia.

Konwencja w opisie wydarzenia (wielkość liter bez znaczenia):
    Klient: Jan Kowalski
    Tel: +48 600 100 200
Wydarzenia bez numeru telefonu są pomijane.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

PHONE_RE = re.compile(r"^\s*(?:tel(?:efon)?|phone)\.?\s*:?\s*(\+?[\d \-()]{9,20})", re.IGNORECASE | re.MULTILINE)
CLIENT_RE = re.compile(r"^\s*klient\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
HTML_TAG_RE = re.compile(r"<[^>]+>")

# Kolory wydarzeń w Google Calendar (colorId) dla wyników
STATUS_COLORS = {
    "CONFIRMED": "10",     # zielony
    "RESCHEDULE": "5",     # żółty
    "CANCELLED": "11",     # czerwony
    "WRONG_PERSON": "8",   # szary
    "UNCLEAR": "6",        # pomarańczowy
    "SMS_SENT": "8",
    "UNREACHED": "8",
    "FAILED": "8",
}

STATUS_LABELS = {
    "CONFIRMED": "potwierdzone",
    "RESCHEDULE": "klient prosi o zmianę terminu",
    "CANCELLED": "klient odwołał",
    "WRONG_PERSON": "zły numer / inna osoba",
    "UNCLEAR": "brak jednoznacznej odpowiedzi",
    "SMS_SENT": "nie odebrał - wysłano SMS",
    "UNREACHED": "nie odebrał",
    "FAILED": "błąd połączenia",
}


@dataclass
class Meeting:
    event_id: str
    calendar_id: str
    start_at: datetime
    phone: str
    client_name: str
    advisor_name: str
    location: str


def normalize_phone(raw: str, default_country: str = "+48") -> str | None:
    """Sprowadza numer do formatu E.164. Zwraca None dla numerów, które nie wyglądają poprawnie."""
    has_plus = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if has_plus:
        number = "+" + digits
    elif digits.startswith("00"):
        number = "+" + digits[2:]
    elif len(digits) == 9:
        number = default_country + digits
    elif len(digits) == 11 and digits.startswith("48"):
        number = "+" + digits
    else:
        return None
    return number if 10 <= len(number) <= 16 else None


def parse_event(event: dict, calendar_id: str, calendar_owner: str) -> Meeting | None:
    if event.get("status") == "cancelled":
        return None
    start = event.get("start", {}).get("dateTime")
    if not start:  # wydarzenia całodniowe
        return None

    description = HTML_TAG_RE.sub("\n", event.get("description") or "")
    phone_match = PHONE_RE.search(description)
    phone = normalize_phone(phone_match.group(1)) if phone_match else None
    if not phone:
        return None

    client_match = CLIENT_RE.search(description)
    client_name = client_match.group(1) if client_match else (event.get("summary") or "").strip()

    organizer = event.get("organizer", {})
    advisor_name = organizer.get("displayName") or calendar_owner

    return Meeting(
        event_id=event["id"],
        calendar_id=calendar_id,
        start_at=datetime.fromisoformat(start).astimezone(timezone.utc),
        phone=phone,
        client_name=client_name,
        advisor_name=advisor_name,
        location=(event.get("location") or "").strip(),
    )


class CalendarClient:
    def __init__(self, credentials_file: str):
        creds = service_account.Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self._owners: dict[str, str] = {}

    def _calendar_owner(self, calendar_id: str) -> str:
        if calendar_id not in self._owners:
            cal = self._service.calendars().get(calendarId=calendar_id).execute()
            self._owners[calendar_id] = cal.get("summary") or "naszym doradcą"
        return self._owners[calendar_id]

    def upcoming_meetings(self, calendar_id: str, time_min: datetime, time_max: datetime) -> list[Meeting]:
        owner = self._calendar_owner(calendar_id)
        meetings: list[Meeting] = []
        page_token = None
        while True:
            resp = self._service.events().list(
                calendarId=calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            ).execute()
            for event in resp.get("items", []):
                meeting = parse_event(event, calendar_id, owner)
                if meeting and time_min <= meeting.start_at <= time_max:
                    meetings.append(meeting)
            page_token = resp.get("nextPageToken")
            if not page_token:
                return meetings

    def write_result(self, calendar_id: str, event_id: str, status: str, note: str, when: datetime) -> None:
        """Koloruje wydarzenie i dopisuje do opisu linię z wynikiem."""
        event = self._service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        line = f"[Potwierdzenie {when:%d.%m %H:%M}] {STATUS_LABELS.get(status, status)}"
        if note:
            line += f" - {note}"
        body = {
            "description": ((event.get("description") or "").rstrip() + "\n\n" + line).strip(),
            "extendedProperties": {"private": {"confirmation_status": status}},
        }
        if status in STATUS_COLORS:
            body["colorId"] = STATUS_COLORS[status]
        self._service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()
