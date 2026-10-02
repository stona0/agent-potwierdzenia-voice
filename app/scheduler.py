"""Pętla, która co minutę pobiera spotkania z kalendarza, dzwoni w odpowiednim momencie,
ponawia nieodebrane połączenia i na koniec wysyła SMS."""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from typing import Protocol

from .config import settings
from .conversation import describe_day
from .db import Call, Database, utcnow

log = logging.getLogger(__name__)

TEST_CALENDAR = "test"


class Dialer(Protocol):
    def start_call(self, to: str, event_id: str) -> str: ...
    def send_sms(self, to: str, body: str) -> bool: ...


class Calendar(Protocol):
    def upcoming_meetings(self, calendar_id: str, time_min: datetime, time_max: datetime) -> list: ...
    def write_result(self, calendar_id: str, event_id: str, status: str, note: str, when: datetime) -> None: ...


class Scheduler:
    def __init__(self, db: Database, calendar: Calendar | None, dialer: Dialer):
        self.db = db
        self.calendar = calendar
        self.dialer = dialer
        self.lead = timedelta(minutes=settings.call_lead_minutes)
        self.min_lead = timedelta(minutes=settings.min_lead_minutes)
        self.retry_delay = timedelta(minutes=settings.retry_delay_minutes)

    async def run_forever(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick, utcnow())
            except Exception:
                log.exception("Błąd w pętli harmonogramu")
            await asyncio.sleep(settings.poll_seconds)

    def tick(self, now: datetime) -> None:
        self.sync_calendar(now)
        self.recover_stale(now)
        self.place_due_calls(now)

    def sync_calendar(self, now: datetime) -> None:
        if not self.calendar:
            return
        # Okno sięga nieco dalej niż czas wyprzedzenia, żeby nie przegapić spotkania między odpytaniami
        time_max = now + self.lead + timedelta(seconds=settings.poll_seconds * 2)
        for calendar_id in settings.calendar_ids:
            try:
                meetings = self.calendar.upcoming_meetings(calendar_id, now + self.min_lead, time_max)
            except Exception:
                log.exception("Nie udało się pobrać wydarzeń z kalendarza %s", calendar_id)
                continue
            for m in meetings:
                self.db.upsert_meeting(
                    event_id=m.event_id, calendar_id=m.calendar_id, start_at=m.start_at, phone=m.phone,
                    client_name=m.client_name, advisor_name=m.advisor_name, location=m.location,
                    first_attempt_at=max(now, m.start_at - self.lead),
                )

    def place_due_calls(self, now: datetime) -> None:
        for call in self.db.due_calls(now):
            if call.start_at - now < self.min_lead:
                self.give_up(call, "za mało czasu do spotkania na kolejną próbę", now)
                continue
            self.place_call(call, now)

    def place_call(self, call: Call, now: datetime) -> None:
        token = secrets.token_urlsafe(24)
        # Status ustawiamy przed wywołaniem Twilio, żeby kolejny tick nie zadzwonił drugi raz
        self.db.update(call.event_id, status="CALLING", attempts=call.attempts + 1, call_token=token,
                       next_attempt_at=now)
        try:
            sid = self.dialer.start_call(call.phone, call.event_id)
        except Exception as e:
            log.exception("Twilio odrzuciło połączenie do %s", call.phone)
            self.handle_unreached(self.db.get(call.event_id), f"błąd Twilio: {e}", now)
            return
        self.db.update(call.event_id, call_sid=sid)
        log.info("Dzwonię do %s (spotkanie %s), próba %d", call.phone, call.event_id, call.attempts + 1)

    def handle_unreached(self, call: Call, reason: str, now: datetime) -> None:
        """Klient nie odebrał / poczta głosowa / błąd - ponawiamy albo wysyłamy SMS."""
        next_try = now + self.retry_delay
        if call.attempts < settings.max_attempts and call.start_at - next_try >= self.min_lead:
            self.db.update(call.event_id, status="RETRY", next_attempt_at=next_try, note=reason)
            log.info("Spotkanie %s: %s - ponowię o %s", call.event_id, reason, next_try)
        else:
            self.give_up(call, reason, now)

    def give_up(self, call: Call, reason: str, now: datetime) -> None:
        sent = False
        try:
            sent = self.dialer.send_sms(call.phone, self.reminder_sms(call, now))
        except Exception:
            log.exception("Nie udało się wysłać SMS do %s", call.phone)
        self.finalize(call, "SMS_SENT" if sent else "UNREACHED", reason, now)

    def finalize(self, call: Call, status: str, note: str, now: datetime, transcript: str | None = None) -> None:
        fields = {"status": status, "note": note}
        if transcript is not None:
            fields["transcript"] = transcript
        self.db.update(call.event_id, **fields)
        log.info("Spotkanie %s: wynik %s (%s)", call.event_id, status, note)
        if self.calendar and call.calendar_id != TEST_CALENDAR:
            try:
                self.calendar.write_result(call.calendar_id, call.event_id, status, note,
                                           now.astimezone(settings.timezone))
            except Exception:
                log.exception("Nie udało się zapisać wyniku w kalendarzu dla %s", call.event_id)

    def recover_stale(self, now: datetime) -> None:
        # Zabezpieczenie na wypadek zgubionego webhooka statusu z Twilio
        for call in self.db.stale_calling(now - timedelta(minutes=15)):
            self.handle_unreached(call, "brak informacji o zakończeniu połączenia", now)

    def reminder_sms(self, call: Call, now: datetime) -> str:
        start_local = call.start_at.astimezone(settings.timezone)
        day = describe_day(start_local, now.astimezone(settings.timezone))
        text = (f"Dzień dobry, przypominamy o spotkaniu z doradcą {call.advisor_name} "
                f"{day} o {start_local:%H:%M}.")
        if settings.contact_phone:
            text += f" Jeśli termin jest nieaktualny, prosimy o kontakt: {settings.contact_phone}."
        return text + f" {settings.company_name}"
