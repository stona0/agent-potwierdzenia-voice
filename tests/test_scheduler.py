from datetime import datetime, timedelta, timezone

from app.db import Database
from app.gcal import Meeting
from app.scheduler import Scheduler

NOW = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)


class FakeDialer:
    def __init__(self):
        self.calls, self.sms = [], []

    def start_call(self, to, event_id):
        self.calls.append((to, event_id))
        return f"CA{len(self.calls)}"

    def send_sms(self, to, body):
        self.sms.append((to, body))
        return True


class FakeCalendar:
    def __init__(self, meetings):
        self.meetings, self.results = meetings, []

    def upcoming_meetings(self, calendar_id, time_min, time_max):
        return [m for m in self.meetings if time_min <= m.start_at <= time_max]

    def write_result(self, calendar_id, event_id, status, note, when):
        self.results.append((event_id, status, note))


def meeting(minutes_ahead, event_id="ev1"):
    return Meeting(event_id, "cal", NOW + timedelta(minutes=minutes_ahead), "+48600100200",
                   "Jan Kowalski", "Anna Nowak", "biuro")


def make(meetings):
    db, dialer, cal = Database(":memory:"), FakeDialer(), FakeCalendar(meetings)
    return db, dialer, cal, Scheduler(db, cal, dialer)


def test_calls_one_hour_before_and_only_once():
    db, dialer, _, s = make([meeting(90)])
    s.tick(NOW)
    assert dialer.calls == []  # jeszcze za wcześnie
    s.tick(NOW + timedelta(minutes=30))
    assert dialer.calls == [("+48600100200", "ev1")]
    s.tick(NOW + timedelta(minutes=31))
    assert len(dialer.calls) == 1
    assert db.get("ev1").status == "CALLING"


def test_retries_then_sends_sms():
    db, dialer, cal, s = make([meeting(60)])
    t = NOW
    s.tick(t)
    for _ in range(5):
        call = db.get("ev1")
        if call.status != "CALLING":
            break
        s.handle_unreached(call, "nie odebrał", t)
        t += timedelta(minutes=10)
        s.tick(t)
    assert len(dialer.calls) == 3
    assert db.get("ev1").status == "SMS_SENT"
    assert dialer.sms and "Anna Nowak" in dialer.sms[0][1]
    assert cal.results == [("ev1", "SMS_SENT", "nie odebrał")]


def test_meeting_too_close_is_skipped():
    db, dialer, _, s = make([meeting(10)])
    s.tick(NOW)
    assert dialer.calls == [] and db.get("ev1") is None


def test_finalize_writes_to_calendar():
    db, dialer, cal, s = make([meeting(60)])
    s.tick(NOW)
    s.finalize(db.get("ev1"), "CONFIRMED", "", NOW)
    assert db.get("ev1").status == "CONFIRMED"
    assert cal.results == [("ev1", "CONFIRMED", "")]


def test_rescheduled_meeting_updates_pending_row():
    db, dialer, cal, s = make([meeting(120)])
    s.sync_calendar(NOW + timedelta(minutes=55))
    cal.meetings = [meeting(150)]
    s.sync_calendar(NOW + timedelta(minutes=89))
    assert db.get("ev1").start_at == NOW + timedelta(minutes=150)
