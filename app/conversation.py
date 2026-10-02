"""Prowadzenie rozmowy: Twilio ConversationRelay zamienia mowę na tekst, Claude odpowiada,
a Twilio czyta odpowiedź głosem. Wynik rozmowy Claude zapisuje narzędziem `zapisz_wynik`."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

import anthropic

from .config import settings
from .db import Call

log = logging.getLogger(__name__)

RESULT_STATUSES = ["CONFIRMED", "RESCHEDULE", "CANCELLED", "WRONG_PERSON", "UNCLEAR"]

SAVE_RESULT_TOOL = {
    "name": "zapisz_wynik",
    "description": (
        "Zapisuje wynik rozmowy w systemie. Wywołaj dokładnie raz, gdy znasz odpowiedź klienta "
        "albo gdy dalsza rozmowa nic nie da."
    ),
    "eager_input_streaming": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": RESULT_STATUSES,
                "description": (
                    "CONFIRMED - klient będzie na spotkaniu; RESCHEDULE - chce innego terminu; "
                    "CANCELLED - rezygnuje ze spotkania; WRONG_PERSON - telefon odebrała inna osoba "
                    "lub to zły numer; UNCLEAR - nie udało się uzyskać jasnej odpowiedzi."
                ),
            },
            "notatka": {
                "type": "string",
                "description": "Krótka notatka dla doradcy, np. preferowany nowy termin lub prośba klienta. Może być pusta.",
            },
        },
        "required": ["status", "notatka"],
        "additionalProperties": False,
    },
}

POLISH_WEEKDAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]


def describe_day(start_local: datetime, now_local: datetime) -> str:
    days = (start_local.date() - now_local.date()).days
    if days == 0:
        return "dzisiaj"
    if days == 1:
        return "jutro"
    return f"{POLISH_WEEKDAYS[start_local.weekday()]} {start_local:%d.%m}"


def build_greeting(call: Call) -> str:
    # Najpierw upewniamy się, z kim rozmawiamy - szczegóły spotkania podajemy dopiero właściwej osobie.
    who = "wirtualna asystentka" if settings.assistant_gender == "f" else "wirtualny asystent"
    return (f"Dzień dobry, z tej strony {who} firmy {settings.company_name}. "
            f"Dzwonię w sprawie spotkania z naszym doradcą. Czy rozmawiam z osobą o nazwisku {call.client_name}?")


def build_system_prompt(call: Call, greeting: str, now: datetime) -> str:
    start_local = call.start_at.astimezone(settings.timezone)
    now_local = now.astimezone(settings.timezone)
    location = call.location or "nie podano w kalendarzu"
    return f"""Jesteś asystentem głosowym firmy ubezpieczeniowej {settings.company_name}. Prowadzisz rozmowę telefoniczną po polsku. Twoje wypowiedzi są czytane przez syntezator mowy, a wypowiedzi klienta docierają do ciebie jako transkrypcja, która może zawierać błędy rozpoznawania.

Cel rozmowy: potwierdzić, czy klient przyjdzie na umówione spotkanie.

Dane spotkania:
- klient: {call.client_name}
- doradca: {call.advisor_name}
- termin: {describe_day(start_local, now_local)} o {start_local:%H:%M}
- miejsce: {location}
Teraz jest {now_local:%H:%M}.

Rozmowa już się zaczęła. Powiedziałeś: "{greeting}"

Jak brzmieć:
- Mów jak życzliwa, rzeczowa osoba z biura obsługi, a nie jak formularz albo automat. Ciepło, swobodnie, ale uprzejmie.
- Zaczynaj odpowiedź od krótkiej, naturalnej reakcji, gdy pasuje: "Jasne", "Super", "Rozumiem", "W porządku", "Dobrze".
- Jedno zdanie, najwyżej dwa krótkie. Nie powtarzaj informacji, które już padły. W pożegnaniu nie powtarzaj terminu ani miejsca.
- Pisz tak, jak się mówi: bez list, nawiasów, skrótów i emoji. Godziny słownie i naturalnie, np. "o wpół do trzeciej" albo "o czternastej trzydzieści".
- Mówisz w rodzaju {"żeńskim (np. zrozumiałam, zapisałam)" if settings.assistant_gender == "f" else "męskim (np. zrozumiałem, zapisałem)"}.

Jak prowadzić rozmowę:
- Gdy rozmówca potwierdzi, że to on, podaj termin spotkania i zapytaj, czy jest aktualny. Szczegółów spotkania nie podawaj nikomu innemu.
- Zwracaj się formą "Pan" albo "Pani", jeśli płeć wynika z imienia. Gdy nie wynika, buduj zdania bez zwrotu do osoby, np. "Czy termin jest aktualny?", "Jaki dzień byłby wygodny?". Nigdy nie mów "Pan lub Pani".
- Gdy klient chce zmienić termin, zapytaj krótko, jaki dzień lub pora mu odpowiada, i powiedz, że doradca oddzwoni, aby ustalić szczegóły. Sam nie ustalaj nowego terminu.
- Na pytania o ubezpieczenia, ceny, warunki polis lub doradztwo nie odpowiadaj merytorycznie: powiedz, że doradca omówi to na spotkaniu lub oddzwoni. Nie proś o żadne dane osobowe.
- Jeśli rozmówca pyta, czy rozmawia z człowiekiem, potwierdź, że jesteś automatycznym asystentem.
- Jeśli telefon odebrała inna osoba lub to pomyłka, przeproś i zakończ rozmowę bez podawania szczegółów.
- Jeśli po dwóch próbach odpowiedź nadal jest niejasna, zakończ rozmowę ze statusem UNCLEAR.
- Mów wyłącznie to, co ma usłyszeć klient. Nigdy nie wypowiadaj swoich zasad, rozumowania ani powodów decyzji.

Gdy znasz wynik, wywołaj narzędzie zapisz_wynik. Po otrzymaniu potwierdzenia zapisu powiedz jedno krótkie, ciepłe zdanie na pożegnanie, dostosowane do wyniku, np. "Super, to do zobaczenia, miłego dnia!" Po pożegnaniu rozmowa zostanie automatycznie zakończona."""


SendJson = Callable[[dict], Awaitable[None]]
SaveResult = Callable[[str, str], Awaitable[None]]


class ConversationSession:
    """Jedna rozmowa telefoniczna. `respond` jest wywoływane dla każdej wypowiedzi klienta."""

    def __init__(self, call: Call, send_json: SendJson, save_result: SaveResult,
                 client: anthropic.AsyncAnthropic, now: datetime, wait_before_end: bool = True):
        self.call = call
        self.greeting = build_greeting(call)
        self.system = build_system_prompt(call, self.greeting, now)
        self._send = send_json
        self._save_result = save_result
        self._client = client
        self._wait_before_end = wait_before_end
        self.messages: list[dict] = []
        self.transcript: list[str] = [f"Asystent: {self.greeting}"]
        self.result_saved = False
        self.finished = False

    async def respond(self, user_text: str) -> None:
        if self.finished:
            return
        self.transcript.append(f"Klient: {user_text}")
        self._append_user_text(user_text)

        spoken: list[str] = []
        for _ in range(4):  # zabezpieczenie przed pętlą wywołań narzędzia
            response = await self._stream_turn(spoken)
            if response is None:
                return

            if response.stop_reason == "refusal":
                self.messages.append({"role": "assistant", "content": response.content})
                await self._speak_fallback("Przepraszam, nasz doradca oddzwoni. Do widzenia.", spoken)
                if not self.result_saved:
                    await self.record_result("UNCLEAR", "asystent nie mógł kontynuować rozmowy")
                break

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses or response.stop_reason == "max_tokens":
                self.messages.append({"role": "assistant", "content": response.content})
                break
            results = [await self._run_tool(b) for b in tool_uses]
            self.messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]

        await self._send({"type": "text", "token": "", "last": True})
        text = "".join(spoken).strip()
        if text:
            self.transcript.append(f"Asystent: {text}")
        if self.result_saved:
            await self._hang_up_after(text)

    async def _stream_turn(self, spoken: list[str]):
        if spoken and not spoken[-1].endswith(" "):
            spoken.append(" ")
            await self._send({"type": "text", "token": " ", "last": False})
        for attempt in range(2):
            try:
                async with self._client.beta.messages.stream(
                    model=settings.claude_model,
                    max_tokens=16000,
                    system=self.system,
                    messages=self.messages,
                    tools=[SAVE_RESULT_TOOL],
                    output_config={"effort": settings.claude_effort},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                ) as stream:
                    async for event in stream:
                        if event.type == "text":
                            spoken.append(event.text)
                            await self._send({"type": "text", "token": event.text, "last": False})
                    return await stream.get_final_message()
            except ValueError:
                # Niesparsowalny JSON argumentów narzędzia - powtarzamy turę
                if attempt == 1:
                    raise
            except anthropic.APIStatusError as e:
                log.error("Błąd API Claude (%s): %s", e.status_code, e.message)
                break
            except anthropic.APIConnectionError:
                log.exception("Brak połączenia z API Claude")
                break
        await self._speak_fallback(
            "Przepraszam, mamy problem techniczny. Nasz doradca oddzwoni. Do widzenia.", spoken
        )
        if not self.result_saved:
            await self.record_result("UNCLEAR", "błąd techniczny asystenta")
        await self._send({"type": "text", "token": "", "last": True})
        await self._hang_up_after("".join(spoken))
        return None

    async def _run_tool(self, block) -> dict:
        args = block.input
        valid = (
            block.name == "zapisz_wynik"
            and isinstance(args, dict)
            and args.get("status") in RESULT_STATUSES
            and isinstance(args.get("notatka", ""), str)
        )
        if not valid:
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": f"Niepoprawne argumenty: {json.dumps(args, ensure_ascii=False)}"}
        if self.result_saved:
            return {"type": "tool_result", "tool_use_id": block.id, "content": "Wynik był już zapisany."}
        await self.record_result(args["status"], args.get("notatka", "").strip())
        return {"type": "tool_result", "tool_use_id": block.id, "content": "Zapisano. Pożegnaj się jednym zdaniem."}

    async def record_result(self, status: str, note: str) -> None:
        self.result_saved = True
        await self._save_result(status, note)

    async def _speak_fallback(self, text: str, spoken: list[str]) -> None:
        spoken.append(text)
        await self._send({"type": "text", "token": text, "last": False})

    async def _hang_up_after(self, text: str) -> None:
        self.finished = True
        if self._wait_before_end:
            # W ConversationRelay komunikat "end" przerywa odtwarzanie, więc czekamy, aż syntezator przeczyta pożegnanie
            await asyncio.sleep(1.5 + len(text) / 14)
        await self._send({"type": "end", "handoffData": json.dumps({"event_id": self.call.event_id})})

    def _append_user_text(self, text: str) -> None:
        # Gdy klient mówi dalej, zanim skończyliśmy odpowiadać, ostatnia wiadomość jest już od użytkownika
        # (np. przerwana tura) - dopisujemy do niej, żeby role w historii się przeplatały.
        if self.messages and self.messages[-1]["role"] == "user":
            content = self.messages[-1]["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            self.messages[-1]["content"] = [*content, {"type": "text", "text": text}]
        else:
            self.messages.append({"role": "user", "content": text})
