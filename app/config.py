import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Brak zmiennej środowiskowej {name} (patrz .env.example)")
    return value


@dataclass(frozen=True)
class Settings:
    # Publiczny adres aplikacji (https), pod który Twilio wysyła webhooki, np. adres z ngrok
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL").rstrip("/"))

    twilio_account_sid: str = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID"))
    twilio_auth_token: str = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN"))
    twilio_from_number: str = field(default_factory=lambda: _env("TWILIO_FROM_NUMBER"))
    # Nadawca SMS-ów awaryjnych (numer lub alfanumeryczny ID); puste = bez SMS
    twilio_sms_from: str = field(default_factory=lambda: os.getenv("TWILIO_SMS_FROM", ""))
    # Głos ElevenLabs; puste = domyślny polski głos Twilio
    tts_voice: str = field(default_factory=lambda: os.getenv("TTS_VOICE", ""))
    # relay = ConversationRelay (płynna rozmowa, wymaga AI/ML Addendum, czyli konta płatnego)
    # gather = <Gather>/<Say> (działa na koncie trial, wolniejsze tury)
    voice_mode: str = field(default_factory=lambda: os.getenv("VOICE_MODE", "relay"))
    # Wykrywanie poczty głosowej (AMD). Na koncie trial wyłącz - komunikat Twilio przed rozmową może je zmylić
    machine_detection: bool = field(default_factory=lambda: os.getenv("MACHINE_DETECTION", "true").lower() == "true")
    # Głos <Say> w trybie gather
    say_voice: str = field(default_factory=lambda: os.getenv("SAY_VOICE", "Google.pl-PL-Chirp3-HD-Aoede"))

    google_credentials_file: str = field(
        default_factory=lambda: _env("GOOGLE_CREDENTIALS_FILE", "credentials/service-account.json")
    )
    calendar_ids: list[str] = field(
        default_factory=lambda: [c.strip() for c in _env("GOOGLE_CALENDAR_IDS").split(",") if c.strip()]
    )

    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5-5"))
    claude_effort: str = field(default_factory=lambda: os.getenv("CLAUDE_EFFORT", "low"))

    company_name: str = field(default_factory=lambda: _env("COMPANY_NAME"))
    # Rodzaj gramatyczny asystenta, zgodny z głosem: f = "asystentka, zrozumiałam", m = "asystent, zrozumiałem"
    assistant_gender: str = field(default_factory=lambda: os.getenv("ASSISTANT_GENDER", "f"))
    # Numer do kontaktu podawany w SMS-ie awaryjnym; puste = SMS bez numeru
    contact_phone: str = field(default_factory=lambda: os.getenv("CONTACT_PHONE", ""))
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw")))

    call_lead_minutes: int = field(default_factory=lambda: int(os.getenv("CALL_LEAD_MINUTES", "60")))
    # Nie dzwonimy, jeśli do spotkania zostało mniej niż tyle minut
    min_lead_minutes: int = field(default_factory=lambda: int(os.getenv("MIN_LEAD_MINUTES", "15")))
    max_attempts: int = field(default_factory=lambda: int(os.getenv("MAX_ATTEMPTS", "3")))
    retry_delay_minutes: int = field(default_factory=lambda: int(os.getenv("RETRY_DELAY_MINUTES", "10")))
    poll_seconds: int = field(default_factory=lambda: int(os.getenv("POLL_SECONDS", "60")))

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "data/calls.db"))
    dashboard_user: str = field(default_factory=lambda: os.getenv("DASHBOARD_USER", "admin"))
    dashboard_password: str = field(default_factory=lambda: _env("DASHBOARD_PASSWORD"))


settings = Settings()
if settings.voice_mode not in ("relay", "gather"):
    raise RuntimeError("VOICE_MODE musi mieć wartość relay albo gather")
if settings.assistant_gender not in ("f", "m"):
    raise RuntimeError("ASSISTANT_GENDER musi mieć wartość f albo m")
