"""Twilio: wykonywanie połączeń, SMS-y awaryjne i TwiML (ConversationRelay albo Gather)."""

import logging
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr

from twilio.request_validator import RequestValidator
from twilio.rest import Client

from .config import settings

log = logging.getLogger(__name__)

_client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
_validator = RequestValidator(settings.twilio_auth_token)


def start_call(to: str, event_id: str) -> str:
    """Rozpoczyna połączenie wychodzące. Zwraca CallSid."""
    options = {}
    if settings.machine_detection:
        options["machine_detection"] = "Enable"  # rozpoznaje pocztę głosową - wtedy rozłączamy i ponawiamy później
    call = _client.calls.create(
        to=to,
        from_=settings.twilio_from_number,
        url=f"{settings.public_base_url}/twilio/voice?event_id={event_id}",
        status_callback=f"{settings.public_base_url}/twilio/status",
        status_callback_event=["completed"],
        timeout=30,
        **options,
    )
    return call.sid


def send_sms(to: str, body: str) -> bool:
    if not settings.twilio_sms_from:
        log.info("TWILIO_SMS_FROM nieustawione - pomijam SMS do %s", to)
        return False
    _client.messages.create(to=to, from_=settings.twilio_sms_from, body=body)
    return True


def is_valid_signature(url: str, params: dict, signature: str) -> bool:
    return _validator.validate(url, params, signature)


def relay_twiml(event_id: str, token: str, greeting: str) -> str:
    ws_url = settings.public_base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/relay"
    voice = f" voice={quoteattr(settings.tts_voice)}" if settings.tts_voice else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Connect action={quoteattr(settings.public_base_url + "/twilio/relay-ended")}>'
        f'<ConversationRelay url={quoteattr(ws_url)} language="pl-PL" ttsProvider="ElevenLabs"{voice}'
        f' transcriptionProvider="Google" welcomeGreeting={quoteattr(greeting)}'
        ' interruptible="speech" welcomeGreetingInterruptible="speech">'
        f'<Parameter name="event_id" value={quoteattr(event_id)}/>'
        f'<Parameter name="token" value={quoteattr(token)}/>'
        "</ConversationRelay></Connect></Response>"
    )


def gather_twiml(event_id: str, token: str, say_text: str, hang_up: bool = False) -> str:
    """Tryb gather: Twilio czyta tekst, nasłuchuje odpowiedzi i wysyła jej transkrypcję na /twilio/gather."""
    say = f'<Say language="pl-PL" voice={quoteattr(settings.say_voice)}>{escape(say_text)}</Say>' if say_text else ""
    if hang_up:
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{say}<Hangup/></Response>'
    action = f"{settings.public_base_url}/twilio/gather?" + urlencode({"event_id": event_id, "token": token})
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather input="speech" language="pl-PL" speechModel="experimental_conversations" speechTimeout="auto"'
        f' timeout="6" actionOnEmptyResult="true" action={quoteattr(action)} method="POST">{say}</Gather>'
        "</Response>"
    )


def hangup_twiml() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
