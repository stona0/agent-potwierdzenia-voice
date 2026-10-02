import asyncio
import json
import socket
import time
from asyncio import sleep as real_sleep
from datetime import timedelta
from urllib.parse import urlencode

import pytest
import uvicorn
from websockets.asyncio.client import connect
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

import app.conversation as conv
import app.main as main
from app.db import utcnow
from tests.test_conversation import FakeClient, FakeStream, text_block, tool_block
from tests.test_scheduler import FakeDialer

BASE = "https://example.ngrok.app"
validator = RequestValidator("test-token")


@pytest.fixture
def client(monkeypatch):
    dialer = FakeDialer()
    monkeypatch.setattr(main.scheduler, "dialer", dialer)
    main.db._conn.execute("DELETE FROM calls")
    c = TestClient(main.app)
    c.dialer = dialer
    return c


def post_twilio(client, path, form):
    sig = validator.compute_signature(BASE + path, form)
    return client.post(path, content=urlencode(form), headers={
        "X-Twilio-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"})


def start_call(client, event_id="ev1"):
    now = utcnow()
    main.db.upsert_meeting(event_id=event_id, calendar_id="test", start_at=now + timedelta(hours=1),
                           phone="+48600100200", client_name="Jan Kowalski", advisor_name="Anna Nowak",
                           location="biuro", first_attempt_at=now)
    main.scheduler.place_call(main.db.get(event_id), now)
    return main.db.get(event_id)


def test_bad_signature_is_rejected(client):
    r = client.post("/twilio/status", data={"CallSid": "CA1"}, headers={"X-Twilio-Signature": "zly"})
    assert r.status_code == 403


def test_voice_webhook_returns_conversation_relay(client):
    call = start_call(client)
    r = post_twilio(client, "/twilio/voice?event_id=ev1", {"CallSid": call.call_sid, "AnsweredBy": "human"})
    assert r.status_code == 200
    assert "<ConversationRelay" in r.text and 'language="pl-PL"' in r.text
    assert "wss://example.ngrok.app/relay" in r.text and call.call_token in r.text


def test_voicemail_hangs_up_and_schedules_retry(client):
    call = start_call(client)
    r = post_twilio(client, "/twilio/voice?event_id=ev1", {"CallSid": call.call_sid, "AnsweredBy": "machine_start"})
    assert "<Hangup/>" in r.text
    assert main.db.get("ev1").status == "RETRY"


def test_no_answer_status_schedules_retry(client):
    call = start_call(client)
    r = post_twilio(client, "/twilio/status", {"CallSid": call.call_sid, "CallStatus": "no-answer"})
    assert r.status_code == 204
    assert main.db.get("ev1").status == "RETRY"


def test_relay_rejects_wrong_token(client):
    start_call(client)
    with client.websocket_connect("/relay") as ws:
        ws.send_json({"type": "setup", "callSid": "CA1", "customParameters": {"event_id": "ev1", "token": "zly"}})
        with pytest.raises(Exception):
            ws.receive_text()


def test_full_conversation_over_websocket(client, monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr(conv.asyncio, "sleep", fast)
    fake = FakeClient([
        FakeStream([], [tool_block("RESCHEDULE", "woli jutro po 16")], "tool_use"),
        FakeStream(["Dobrze, doradca oddzwoni. Do widzenia."], [text_block("...")], "end_turn"),
    ])
    monkeypatch.setattr(main, "claude", fake)
    call = start_call(client)

    with client.websocket_connect("/relay") as ws:
        ws.send_json({"type": "setup", "callSid": call.call_sid,
                      "customParameters": {"event_id": "ev1", "token": call.call_token}})
        ws.send_json({"type": "prompt", "voicePrompt": "Dziś nie dam rady, może jutro po szesnastej", "last": True})
        received = []
        while not received or received[-1]["type"] != "end":
            received.append(ws.receive_json())

    row = main.db.get("ev1")
    assert row.status == "RESCHEDULE" and row.note == "woli jutro po 16"
    assert "Klient: Dziś nie dam rady" in row.transcript
    assert any(m.get("token", "").startswith("Dobrze") for m in received)


class SlowStream(FakeStream):
    async def get_final_message(self):
        await real_sleep(0.3)
        return await super().get_final_message()


def test_relay_over_uvicorn_saves_result_when_text_precedes_tool_use_after_hangup(client, monkeypatch):
    async def fast(_):
        await real_sleep(0)
    monkeypatch.setattr(conv.asyncio, "sleep", fast)
    fake = FakeClient([
        SlowStream(["Dobrze, ", "zapisuję."], [text_block("..."), tool_block("CONFIRMED", "będzie")], "tool_use"),
        FakeStream(["Do widzenia."], [text_block("...")], "end_turn"),
    ])
    monkeypatch.setattr(main, "claude", fake)
    call = start_call(client)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    async def go():
        server = uvicorn.Server(uvicorn.Config(main.app, port=port, lifespan="off", log_level="error"))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await real_sleep(0.01)
        async with connect(f"ws://127.0.0.1:{port}/relay") as ws:
            await ws.send(json.dumps({"type": "setup", "callSid": call.call_sid,
                                      "customParameters": {"event_id": "ev1", "token": call.call_token}}))
            await ws.send(json.dumps({"type": "prompt", "voicePrompt": "Tak, będę", "last": True}))
        deadline = time.monotonic() + 5
        while not main.db.get("ev1").transcript and time.monotonic() < deadline:
            await real_sleep(0.02)
        server.should_exit = True
        await serving

    asyncio.run(go())
    row = main.db.get("ev1")
    assert (row.status, row.note) == ("CONFIRMED", "będzie")


@pytest.fixture
def gather_mode(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(main, "settings", replace(main.settings, voice_mode="gather"))
    main.gather_calls.clear()


def gather_path(call):
    return f"/twilio/gather?{urlencode({'event_id': call.event_id, 'token': call.call_token})}"


def test_gather_mode_voice_webhook_returns_gather(client, gather_mode):
    call = start_call(client)
    r = post_twilio(client, "/twilio/voice?event_id=ev1", {"CallSid": call.call_sid, "AnsweredBy": "human"})
    assert "<Gather" in r.text and 'language="pl-PL"' in r.text and "<ConversationRelay" not in r.text
    assert "Dzień dobry" in r.text and call.call_token in r.text


def test_gather_mode_full_conversation(client, gather_mode, monkeypatch):
    fake = FakeClient([
        FakeStream(["Spotkanie z doradcą Anną Nowak jest o jedenastej. Czy aktualne?"], [text_block("...")], "end_turn"),
        FakeStream([], [tool_block("CONFIRMED")], "tool_use"),
        FakeStream(["Dziękuję, do widzenia."], [text_block("...")], "end_turn"),
    ])
    monkeypatch.setattr(main, "claude", fake)
    call = start_call(client)
    post_twilio(client, "/twilio/voice?event_id=ev1", {"CallSid": call.call_sid, "AnsweredBy": "human"})

    r1 = post_twilio(client, gather_path(call), {"CallSid": call.call_sid, "SpeechResult": "Tak, to ja"})
    assert "Czy aktualne?" in r1.text and "<Gather" in r1.text

    r2 = post_twilio(client, gather_path(call), {"CallSid": call.call_sid, "SpeechResult": "Tak, będę"})
    assert "Dziękuję, do widzenia." in r2.text and "<Hangup/>" in r2.text
    assert main.db.get("ev1").status == "CONFIRMED"

    post_twilio(client, "/twilio/status", {"CallSid": call.call_sid, "CallStatus": "completed"})
    row = main.db.get("ev1")
    assert row.status == "CONFIRMED" and "Klient: Tak, będę" in row.transcript
    assert "ev1" not in main.gather_calls


def test_gather_mode_silence_twice_ends_as_unclear(client, gather_mode):
    call = start_call(client)
    post_twilio(client, "/twilio/voice?event_id=ev1", {"CallSid": call.call_sid, "AnsweredBy": "human"})
    r1 = post_twilio(client, gather_path(call), {"CallSid": call.call_sid, "SpeechResult": ""})
    assert "<Gather" in r1.text
    r2 = post_twilio(client, gather_path(call), {"CallSid": call.call_sid})
    assert "<Hangup/>" in r2.text
    assert main.db.get("ev1").status == "UNCLEAR"


def test_gather_rejects_wrong_token(client, gather_mode):
    call = start_call(client)
    post_twilio(client, "/twilio/voice?event_id=ev1", {"CallSid": call.call_sid, "AnsweredBy": "human"})
    path = f"/twilio/gather?{urlencode({'event_id': 'ev1', 'token': 'zly'})}"
    r = post_twilio(client, path, {"CallSid": call.call_sid, "SpeechResult": "Tak"})
    assert "<Hangup/>" in r.text and "<Say" not in r.text


def test_test_call_rejects_cross_origin_post(client):
    form = {"phone": "+44 900 123 4567", "client_name": "X", "minutes": "60"}
    r = client.post("/test-call", data=form, auth=("admin", "haslo"),
                    headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    assert client.dialer.calls == []

    r = client.post("/test-call", data=form, auth=("admin", "haslo"),
                    headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    assert [c[0] for c in client.dialer.calls] == ["+449001234567"]
