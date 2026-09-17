"""Telegram stand-ins shared by tests that must not import another test script."""
import types

from server.vision import Verdict


class FakeMessage:
    def __init__(self, text=None, chat_id=1):
        self.text = text
        self.caption = None
        self.replies = []
        self.photo = []
        self.chat = types.SimpleNamespace(id=chat_id, send_action=self._noop)

    async def _noop(self, *_a, **_k):
        return None

    async def reply_text(self, t, **_kw):
        self.replies.append(t)

    @property
    def last(self):
        return self.replies[-1] if self.replies else ""


class FakeUpdate:
    def __init__(self, text=None, chat_id=1):
        self.message = FakeMessage(text, chat_id)
        self.effective_chat = types.SimpleNamespace(id=chat_id)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **_kw):
        self.sent.append((chat_id, text))

    def to(self, chat_id):
        return [t for c, t in self.sent if c == chat_id]


class StubValidator:
    def __init__(self):
        self.verdict = Verdict(ok=True, reason="ok", failure_code="ok")

    async def check(self, *_a, **_kw):
        return self.verdict
