import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.conversation as conv
from app.db import Call

NOW = datetime(2026, 10, 2, 11, 30, tzinfo=timezone.utc)


def make_call():
    return Call("ev1", "cal", NOW + timedelta(hours=1), "+48600100200", "Jan Kowalski", "Anna Nowak",
                "biuro", "CALLING", 1, NOW, "CA1", "tok", "", "", NOW)


class FakeStream:
    def __init__(self, texts, content, stop_reason):
        self.texts, self.content, self.stop_reason = texts, content, stop_reason

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for t in self.texts:
            yield SimpleNamespace(type="text", text=t)

    async def get_final_message(self):
        return SimpleNamespace(content=self.content, stop_reason=self.stop_reason)


class FakeClient:
    def __init__(self, turns):
        self.turns, self.requests = list(turns), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.turns.pop(0)


def text_block(t):
    return SimpleNamespace(type="text", text=t)


def tool_block(status, note=""):
    return SimpleNamespace(type="tool_use", id="tu1", name="zapisz_wynik", input={"status": status, "notatka": note})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr(conv.asyncio, "sleep", fast)


def run_session(turns, prompts):
    sent, saved = [], []

    async def send(p):
        sent.append(p)

    async def save(status, note):
        saved.append((status, note))

    client = FakeClient(turns)
    session = conv.ConversationSession(make_call(), send, save, client, NOW)

    async def go():
        for p in prompts:
            await session.respond(p)

    asyncio.run(go())
    return session, sent, saved, client


def test_confirmation_flow_saves_result_and_hangs_up():
    turns = [
        FakeStream(["Dzień dobry panie Janie. ", "Czy spotkanie o czternastej trzydzieści jest aktualne?"],
                   [text_block("...")], "end_turn"),
        FakeStream([], [tool_block("CONFIRMED")], "tool_use"),
        FakeStream(["Dziękuję, do zobaczenia. Do widzenia."], [text_block("...")], "end_turn"),
    ]
    session, sent, saved, client = run_session(turns, ["Tak, to ja.", "Tak, będę."])

    assert saved == [("CONFIRMED", "")]
    assert sent[-1]["type"] == "end"
    assert any(p.get("last") is True for p in sent)
    assert session.finished
    # Historia: user, assistant, user, assistant(tool_use), user(tool_result), assistant
    roles = [m["role"] for m in client.requests[-1]["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert client.requests[0]["fallbacks"] == "default"
    assert "Jan Kowalski" in client.requests[0]["system"]
    assert "Klient: Tak, będę." in session.transcript


def test_invalid_tool_input_returns_error_and_does_not_save():
    bad = SimpleNamespace(type="tool_use", id="tu1", name="zapisz_wynik", input={"status": "MAYBE"})
    turns = [
        FakeStream([], [bad], "tool_use"),
        FakeStream(["Przepraszam, czy mógłby Pan powtórzyć?"], [text_block("...")], "end_turn"),
    ]
    session, sent, saved, client = run_session(turns, ["no nie wiem"])
    assert saved == []
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert not session.finished


def test_refusal_ends_call_as_unclear():
    turns = [FakeStream([], [], "refusal")]
    session, sent, saved, _ = run_session(turns, ["halo?"])
    assert saved == [("UNCLEAR", "asystent nie mógł kontynuować rozmowy")]
    assert sent[-1]["type"] == "end"


def test_consecutive_user_prompts_are_merged():
    session = conv.ConversationSession(make_call(), None, None, None, NOW)
    session._append_user_text("Tak")
    session._append_user_text("to ja")
    assert session.messages == [{"role": "user", "content": [{"type": "text", "text": "Tak"},
                                                              {"type": "text", "text": "to ja"}]}]


def test_cancel_during_save_keeps_tool_use_paired_with_result():
    saving, release = asyncio.Event(), asyncio.Event()

    async def send(_):
        return None

    async def save(status, note):
        saving.set()
        await release.wait()

    client = FakeClient([
        FakeStream([], [tool_block("CONFIRMED")], "tool_use"),
        FakeStream(["Rozumiem."], [text_block("...")], "end_turn"),
    ])
    session = conv.ConversationSession(make_call(), send, save, client, NOW)

    async def go():
        task = asyncio.create_task(session.respond("Tak, będę"))
        await saving.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await session.respond("a jeszcze jedno")

    asyncio.run(go())
    assert client.requests[-1]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Tak, będę"},
                                     {"type": "text", "text": "a jeszcze jedno"}]},
    ]
