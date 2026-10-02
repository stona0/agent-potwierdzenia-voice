# Agent potwierdzeń spotkań

Godzinę przed spotkaniem z Google Calendar agent dzwoni do klienta, rozmawia po polsku i sprawdza, czy klient przyjdzie. Wynik zapisuje w kalendarzu: koloruje wydarzenie i dopisuje notatkę do opisu.

```
Google Calendar ──(co minutę)──► harmonogram ──► Twilio: połączenie wychodzące
                                                   │
                       ConversationRelay (mowa ⇄ tekst, polski głos ElevenLabs)
                                                   │ WebSocket
                                             Claude: rozmowa + narzędzie zapisz_wynik
                                                   │
                              SQLite + panel WWW + kolor i notatka w kalendarzu
```

**Wyniki rozmowy**
- `CONFIRMED`: klient potwierdził (zielony).
- `RESCHEDULE`: chce zmienić termin; preferencja trafia do notatki (żółty).
- `CANCELLED`: klient odwołał (czerwony).
- `WRONG_PERSON`: odebrała inna osoba.
- `UNCLEAR`: brak jasnej odpowiedzi (pomarańczowy).
- `SMS_SENT` lub `UNREACHED`: klient nie odebrał mimo ponowień.

**Ponowienia.** Poczta głosowa albo brak odpowiedzi oznacza kolejną próbę po 10 minutach, najwyżej 3 próby. Na mniej niż 15 minut przed spotkaniem system przestaje dzwonić i wysyła SMS z przypomnieniem.

## 1. Jak wpisywać spotkania w kalendarzu

W **opisie** wydarzenia muszą być takie linie:

```
Klient: Jan Kowalski
Tel: 600 100 200
```

Wydarzenia bez numeru telefonu są pomijane. Gdy brakuje linii `Klient:`, agent użyje tytułu wydarzenia jako nazwy klienta. Pole „Lokalizacja” agent odczyta klientowi jako miejsce spotkania.

## 2. Konfiguracja Twilio

Aplikacja ma dwa tryby rozmowy (`VOICE_MODE` w `.env`):

| | `gather` | `relay` |
|---|---|---|
| Konto trial | działa | nie działa (wymaga AI/ML Addendum) |
| Płynność | pauza 1–3 s po każdej wypowiedzi klienta | odpowiedź zaczyna się niemal od razu |
| Głos | Google Chirp3-HD lub Amazon Polly | ElevenLabs |

Na trialu ustaw `VOICE_MODE=gather`. Po przejściu na płatne konto przełącz się na `relay`, nic więcej nie trzeba zmieniać.

**Konto trial:**
- dzwoni tylko na numery w kraju rejestracji, więc na polskie numery, jeśli konto zakładano w Polsce;
- dzwoni tylko na numery dodane w *Verified Caller IDs* (Twój numer z rejestracji jest dodany automatycznie);
- przed rozmową odtwarza komunikat Twilio i prosi o **naciśnięcie dowolnego klawisza**. Dopiero potem włącza się asystent.
- ten komunikat może zmylić wykrywanie poczty głosowej, dlatego na trialu ustaw `MACHINE_DETECTION=false`.

**Konto płatne (tryb `relay`):**

1. **Console → Voice → Settings → General:** zaakceptuj *Predictive and Generative AI/ML Features Addendum*. Bez tego ConversationRelay nie działa.
2. **Voice → Settings → Geo permissions:** włącz **Poland**.
3. **Numer:** kup polski numer (wymaga *Regulatory Bundle*, czyli danych firmy). Do testów może być dowolny numer Twilio.
4. **SMS (opcjonalnie):** w `TWILIO_SMS_FROM` wpisz numer z obsługą SMS albo alfanumeryczną nazwę nadawcy, np. `Polisa`.

## 3. Konfiguracja Google Calendar

1. W [Google Cloud Console](https://console.cloud.google.com/) utwórz projekt i włącz **Google Calendar API**.
2. **IAM → Service Accounts:** utwórz konto usługi, potem **Keys → Add key → JSON**. Zapisz plik jako `credentials/service-account.json`.
3. Każdy doradca otwiera w Google Calendar **Ustawienia kalendarza → Udostępnij określonym osobom**, dodaje adres e-mail konta usługi (`...@...iam.gserviceaccount.com`) i nadaje uprawnienie **„Wprowadzanie zmian w wydarzeniach”**.
4. W `.env` wpisz w `GOOGLE_CALENDAR_IDS` identyfikatory tych kalendarzy (zwykle adres e-mail doradcy), rozdzielone przecinkami.

## 4. Uruchomienie lokalne

```bash
cp .env.example .env
```

Uzupełnij `.env`, potem:

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
```

Twilio musi widzieć aplikację z internetu. W osobnym terminalu:

```bash
ngrok http 8000
```

Adres `https://....ngrok-free.app` wpisz do `PUBLIC_BASE_URL` w `.env`. Potem uruchom serwer:

```bash
.venv/bin/uvicorn app.main:app --port 8000
```

Otwórz http://localhost:8000 (login i hasło z `.env`). Formularz **„Zadzwoń testowo”** dzwoni od razu na podany numer i symuluje spotkanie z doradcą „Anna Nowak” za godzinę. Tak sprawdzisz rozmowę bez dodawania wydarzeń w kalendarzu.

Testy automatyczne:

```bash
.venv/bin/python -m pytest
```

## 5. Produkcja

- Uruchom kontener (`docker build -t potwierdzenia . && docker run -p 8000:8000 --env-file .env -v $PWD/data:/srv/data -v $PWD/credentials:/srv/credentials potwierdzenia`) za reverse proxy z HTTPS, np. Caddy. Proxy musi przepuszczać WebSockety.
- Aplikacja działa w **jednej instancji**: harmonogram jest wbudowany w proces. Nie uruchamiaj kilku kopii naraz, bo klienci dostaliby kilka telefonów.
- Domyślny model to `claude-opus-5-5` z `CLAUDE_EFFORT=low`. Jeśli odpowiedzi w rozmowie są za wolne, ustaw `CLAUDE_MODEL=claude-sonnet-5-5`.

## Kwestie prawne (do weryfikacji z prawnikiem firmy)

- Agent przedstawia się jako automatyczny asystent (AI Act, art. 50).
- Przy umawianiu spotkania zbieraj zgodę klienta na kontakt telefoniczny i SMS w sprawie spotkania (Prawo komunikacji elektronicznej, RODO).
- Szczegóły spotkania agent podaje dopiero po potwierdzeniu, że rozmawia z właściwą osobą. Nie udziela informacji o produktach (IDD).
- Transkrypcje rozmów są przechowywane w `data/calls.db`. Ustal okres retencji i ujmij Twilio, Google i Anthropic w rejestrze podmiotów przetwarzających.

## Struktura

| Plik | Rola |
|---|---|
| `app/main.py` | FastAPI: webhooki Twilio (z weryfikacją podpisu), WebSocket rozmowy, panel |
| `app/scheduler.py` | Pobieranie spotkań, dzwonienie, ponowienia, SMS, zapis wyniku |
| `app/conversation.py` | Prompt i obsługa rozmowy z Claude (streaming, narzędzie `zapisz_wynik`) |
| `app/gcal.py` | Google Calendar: odczyt spotkań, parsowanie opisu, zapis wyniku |
| `app/telephony.py` | Twilio: połączenia, SMS, TwiML ConversationRelay |
| `app/db.py` | SQLite |
