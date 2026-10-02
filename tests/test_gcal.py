from datetime import timezone

from app.gcal import normalize_phone, parse_event


def test_normalize_phone():
    assert normalize_phone("600 100 200") == "+48600100200"
    assert normalize_phone("+48 600-100-200") == "+48600100200"
    assert normalize_phone("0048600100200") == "+48600100200"
    assert normalize_phone("48600100200") == "+48600100200"
    assert normalize_phone("12345") is None


def event(description, **extra):
    return {
        "id": "ev1",
        "summary": "Spotkanie OC/AC",
        "description": description,
        "start": {"dateTime": "2026-10-02T14:30:00+02:00"},
        "organizer": {"displayName": "Anna Nowak"},
        "location": "ul. Długa 5",
        **extra,
    }


def test_parse_event_reads_phone_and_client():
    m = parse_event(event("Klient: Jan Kowalski\nTel: 600 100 200\nOC samochodu"), "cal", "Kalendarz")
    assert m.phone == "+48600100200"
    assert m.client_name == "Jan Kowalski"
    assert m.advisor_name == "Anna Nowak"
    assert m.start_at.tzinfo == timezone.utc and m.start_at.hour == 12


def test_parse_event_html_description_and_fallback_name():
    m = parse_event(event("<b>Telefon:</b> +48 600 100 200<br>2 auta"), "cal", "Kalendarz")
    assert m.phone == "+48600100200"
    assert m.client_name == "Spotkanie OC/AC"


def test_parse_event_skips_without_phone_or_cancelled():
    assert parse_event(event("bez numeru"), "cal", "K") is None
    assert parse_event(event("Tel: 600100200", status="cancelled"), "cal", "K") is None
    assert parse_event(event("Tel: 600100200", start={"date": "2026-10-02"}), "cal", "K") is None


def test_parse_event_takes_phone_only_from_tel_line():
    m = parse_event(event("Polisa nr 123456789\nKlient: Jan\nTel: 600 100 200"), "cal", "K")
    assert m.phone == "+48600100200"
    m = parse_event(event("Polisa nr 1234567890123\nTel: 600 100 200"), "cal", "K")
    assert m.phone == "+48600100200"
    m = parse_event(event("Hotel 123456789 parking\nTel: 600100200"), "cal", "K")
    assert m.phone == "+48600100200"
