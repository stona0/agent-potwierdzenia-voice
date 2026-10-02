import asyncio
import contextlib
import json
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import anthropic
from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.websockets import WebSocketDisconnected

from . import telephony
from .config import settings
from .conversation import ConversationSession, build_greeting
from .db import Database, utcnow
from .gcal import STATUS_LABELS, CalendarClient, normalize_phone
from .scheduler import TEST_CALENDAR, Scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

db = Database(settings.db_path)
claude = anthropic.AsyncAnthropic()


@dataclass
class GatherCall:
    """Stan rozmowy w trybie gather - między kolejnymi webhookami Twilio trzymamy go w pamięci."""
    session: ConversationSession
    outbox: list[dict] = field(default_factory=list)
    silences: int = 0


gather_calls: dict[str, GatherCall] = {}
# Twilio czeka na odpowiedź webhooka maks. 15 s
GATHER_REPLY_TIMEOUT = 12
RELAY_DRAIN_TIMEOUT = 30


def _make_calendar() -> CalendarClient | None:
    if not Path(settings.google_credentials_file).exists():
        log.warning("Brak pliku %s - działam bez Google Calendar (tylko połączenia testowe)",
                    settings.google_credentials_file)
        return None
    return CalendarClient(settings.google_credentials_file)


scheduler = Scheduler(db, _make_calendar(), telephony)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scheduler.run_forever())
    yield
    task.cancel()


app = FastAPI(title="Agent potwierdzeń spotkań", lifespan=lifespan)
security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    ok_user = secrets.compare_digest(credentials.username, settings.dashboard_user)
    ok_pass = secrets.compare_digest(credentials.password, settings.dashboard_password)
    if not (ok_user and ok_pass):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})


async def _parse_twilio(request: Request, require_signature: bool) -> dict:
    form = dict(await request.form())
    signature = request.headers.get("X-Twilio-Signature")
    url = settings.public_base_url + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    if signature is None and not require_signature:
        return form
    if not telephony.is_valid_signature(url, form, signature or ""):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Niepoprawny podpis Twilio")
    return form


async def twilio_form(request: Request) -> dict:
    return await _parse_twilio(request, require_signature=True)


async def twilio_call_form(request: Request) -> dict:
    """Webhook trwającej rozmowy. Bramka konta trial ("press any key") wysyła go bez podpisu,
    dlatego handler musi sam sprawdzić, że CallSid należy do połączenia, które rozpoczęliśmy."""
    return await _parse_twilio(request, require_signature=False)


def _is_our_call(call_sid: str | None, form: dict) -> bool:
    return bool(call_sid) and secrets.compare_digest(call_sid, form.get("CallSid", ""))


def twiml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


# --- Webhooki Twilio -------------------------------------------------------------------------

def _result_saver(call):
    async def save_result(result_status: str, note: str) -> None:
        await asyncio.to_thread(scheduler.finalize, call, result_status, note, utcnow())
    return save_result


@app.post("/twilio/voice")
async def twilio_voice(event_id: str, form: dict = Depends(twilio_call_form)):
    call = db.get(event_id)
    if not call or call.status != "CALLING" or not _is_our_call(call.call_sid, form):
        return twiml(telephony.hangup_twiml())

    answered_by = form.get("AnsweredBy", "")
    if answered_by.startswith("machine") or answered_by == "fax":
        await asyncio.to_thread(scheduler.handle_unreached, call, "poczta głosowa", utcnow())
        return twiml(telephony.hangup_twiml())

    if settings.voice_mode == "relay":
        return twiml(telephony.relay_twiml(call.event_id, call.call_token, build_greeting(call)))

    outbox: list[dict] = []

    async def collect(payload: dict) -> None:
        outbox.append(payload)

    session = ConversationSession(call, collect, _result_saver(call), claude, utcnow(), wait_before_end=False)
    gather_calls[call.event_id] = GatherCall(session=session, outbox=outbox)
    return twiml(telephony.gather_twiml(call.event_id, call.call_token, session.greeting))


@app.post("/twilio/gather")
async def twilio_gather(event_id: str, token: str, form: dict = Depends(twilio_call_form)):
    state = gather_calls.get(event_id)
    if (not state or not secrets.compare_digest(state.session.call.call_token or "", token)
            or not _is_our_call(state.session.call.call_sid, form)):
        return twiml(telephony.hangup_twiml())
    session = state.session
    if form.get("CallStatus") == "completed":
        return twiml(telephony.hangup_twiml())
    speech = (form.get("SpeechResult") or "").strip()

    if not speech:
        state.silences += 1
        if state.silences >= 2:
            if not session.result_saved:
                await session.record_result("UNCLEAR", "brak odpowiedzi klienta")
            return twiml(telephony.gather_twiml(
                event_id, token, "Nie słyszę odpowiedzi. Nasz doradca skontaktuje się z Państwem. Do widzenia.",
                hang_up=True))
        return twiml(telephony.gather_twiml(event_id, token, "Halo? Czy mnie słychać?"))

    state.silences = 0
    state.outbox.clear()
    try:
        await asyncio.wait_for(session.respond(speech), GATHER_REPLY_TIMEOUT)
    except TimeoutError:
        log.warning("Claude nie odpowiedział w %ss (spotkanie %s)", GATHER_REPLY_TIMEOUT, event_id)
        return twiml(telephony.gather_twiml(event_id, token, "Przepraszam, mogę prosić jeszcze raz?"))

    reply = "".join(m.get("token", "") for m in state.outbox if m["type"] == "text").strip()
    ended = any(m["type"] == "end" for m in state.outbox)
    return twiml(telephony.gather_twiml(event_id, token, reply, hang_up=ended))


@app.post("/twilio/status")
async def twilio_status(form: dict = Depends(twilio_form)):
    call = db.get_by_call_sid(form.get("CallSid", ""))
    if call and (state := gather_calls.pop(call.event_id, None)):
        db.update(call.event_id, transcript="\n".join(state.session.transcript))
        call = db.get(call.event_id)
    if not call or call.status != "CALLING":
        return Response(status_code=204)

    call_status = form.get("CallStatus")
    now = utcnow()
    reasons = {"no-answer": "nie odebrał", "busy": "zajęte", "failed": "połączenie nieudane", "canceled": "anulowane"}
    if call_status in reasons:
        await asyncio.to_thread(scheduler.handle_unreached, call, reasons[call_status], now)
    elif call_status == "completed":
        # Rozmowa się odbyła, ale bez zapisanego wyniku - klient rozłączył się wcześniej
        await asyncio.to_thread(scheduler.finalize, call, "UNCLEAR", "klient rozłączył się przed odpowiedzią", now)
    return Response(status_code=204)


@app.post("/twilio/relay-ended")
async def relay_ended(form: dict = Depends(twilio_form)):
    if form.get("SessionStatus") == "failed":
        log.error("ConversationRelay zakończony błędem: %s %s", form.get("ErrorCode"), form.get("ErrorMessage"))
    return twiml(telephony.hangup_twiml())


@app.websocket("/relay")
async def relay(ws: WebSocket):
    await ws.accept()
    session: ConversationSession | None = None
    current: asyncio.Task | None = None

    async def send_if_connected(payload: dict) -> None:
        with contextlib.suppress(WebSocketDisconnect, WebSocketDisconnected):
            await ws.send_text(json.dumps(payload, ensure_ascii=False))

    try:
        async for raw in ws.iter_text():
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "setup":
                params = msg.get("customParameters") or {}
                call = db.get(params.get("event_id", ""))
                if not call or not call.call_token or not secrets.compare_digest(call.call_token, params.get("token", "")):
                    log.warning("Odrzucam sesję ConversationRelay z niepoprawnym tokenem")
                    await ws.close()
                    return

                session = ConversationSession(call, send_if_connected, _result_saver(call), claude, utcnow())
            elif kind == "prompt" and session and not session.finished:
                if current and not current.done():
                    # Klient mówi dalej - porzucamy niedokończoną odpowiedź i odpowiadamy na całość
                    current.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await current
                current = asyncio.create_task(session.respond(msg.get("voicePrompt", "")))
            elif kind == "error":
                log.error("ConversationRelay: %s", msg.get("description"))
    except WebSocketDisconnect:
        pass
    finally:
        if current and not current.done():
            done, _ = await asyncio.wait({current}, timeout=RELAY_DRAIN_TIMEOUT)
            if not done:
                current.cancel()
        if session:
            db.update(session.call.event_id, transcript="\n".join(session.transcript))


# --- Panel -----------------------------------------------------------------------------------

STATUS_STYLE = {
    "CONFIRMED": "#1a7f37", "RESCHEDULE": "#9a6700", "CANCELLED": "#cf222e", "UNCLEAR": "#bc4c00",
    "CALLING": "#0969da", "RETRY": "#0969da",
}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def dashboard():
    rows = []
    for c in db.recent(utcnow() - timedelta(days=2)):
        start = c.start_at.astimezone(settings.timezone)
        label = STATUS_LABELS.get(c.status, {"PENDING": "oczekuje", "RETRY": "ponowienie", "CALLING": "trwa połączenie"}.get(c.status, c.status))
        color = STATUS_STYLE.get(c.status, "#57606a")
        rows.append(
            f"<tr><td>{start:%d.%m %H:%M}</td><td>{escape(c.client_name)}</td><td>{escape(c.phone)}</td>"
            f"<td>{escape(c.advisor_name)}</td><td style='color:{color};font-weight:600'>{escape(label)}</td>"
            f"<td>{c.attempts}</td><td>{escape(c.note)}</td>"
            f"<td><details><summary>pokaż</summary><pre>{escape(c.transcript)}</pre></details></td></tr>"
        )
    table = "\n".join(rows) or "<tr><td colspan=8>Brak spotkań</td></tr>"
    return f"""<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="30">
<title>Potwierdzenia spotkań</title>
<style>
body{{font-family:system-ui,sans-serif;margin:24px;color:#1f2328;background:#fff}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{border-bottom:1px solid #d0d7de;padding:8px;text-align:left;vertical-align:top}}
pre{{white-space:pre-wrap;max-width:420px}} form{{margin:16px 0;display:flex;gap:8px;flex-wrap:wrap}}
input,button{{padding:6px 10px;font-size:14px}}
</style></head><body>
<h1>Potwierdzenia spotkań</h1>
<form method="post" action="/test-call">
  <input name="phone" placeholder="Numer telefonu" required>
  <input name="client_name" placeholder="Imię i nazwisko" required>
  <input name="minutes" type="number" value="60" min="20" title="Za ile minut spotkanie">
  <button>Zadzwoń testowo</button>
</form>
<table><thead><tr><th>Spotkanie</th><th>Klient</th><th>Telefon</th><th>Doradca</th><th>Status</th>
<th>Próby</th><th>Notatka</th><th>Transkrypcja</th></tr></thead><tbody>{table}</tbody></table>
</body></html>"""


@app.post("/test-call", dependencies=[Depends(require_admin)])
async def test_call(request: Request, phone: str = Form(...), client_name: str = Form(...), minutes: int = Form(60)):
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Niedozwolone źródło żądania")
    number = normalize_phone(phone)
    if not number:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Niepoprawny numer telefonu")
    now = utcnow()
    event_id = f"test-{secrets.token_hex(6)}"
    db.upsert_meeting(
        event_id=event_id, calendar_id=TEST_CALENDAR, start_at=now + timedelta(minutes=max(minutes, 20)),
        phone=number, client_name=client_name, advisor_name="Anna Nowak", location="biuro przy ulicy Długiej 5",
        first_attempt_at=now,
    )
    await asyncio.to_thread(scheduler.place_call, db.get(event_id), now)
    return RedirectResponse("/", status_code=303)
