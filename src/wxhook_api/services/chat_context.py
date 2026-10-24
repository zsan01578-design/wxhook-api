"""Bounded, process-local question/answer history with explicit group ownership."""
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import secrets
import time
from typing import Callable


MAX_TURNS = 6
MAX_CONTEXT_CHARS = 12000
MAX_SESSIONS = 100
CONTEXT_TTL = 30 * 60


@dataclass
class ConversationTurn:
    account: str
    group: str
    sender: str
    question: str
    provider: object | None = None
    generated: bool = False
    history_senders: frozenset[str] = field(default_factory=frozenset)
    image_prompt: str = ""
    allowed_sender: Callable[[str], bool] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ConversationView:
    participant: str
    history: Callable[[], list[dict]]


@dataclass(frozen=True)
class Exchange:
    sender: str
    question: str
    answer: str
    history_senders: frozenset[str] = field(default_factory=frozenset)


@dataclass
class Session:
    # Keeping the provider reference also prevents a recycled object id from
    # exposing a previous model's session to a newly created provider.
    provider: object
    updated_at: float
    exchanges: list[Exchange] = field(default_factory=list)


class GroupConversations:
    def __init__(self, *, now: Callable[[], float] = time.monotonic):
        self._now = now
        self._sessions: OrderedDict[tuple[str, str, int], Session] = OrderedDict()
        self._participant_secret = secrets.token_bytes(32)

    def participant_id(self, account: str, group: str, sender: str) -> str:
        if sender in {"", "0", group, "notify@all"}:
            return "unknown"
        identity = json.dumps([account, group, sender], ensure_ascii=False).encode("utf-8")
        return "member_" + hmac.new(self._participant_secret, identity, hashlib.sha256).hexdigest()[:24]

    def view(self, turn: ConversationTurn, provider: object,
             *, allowed_sender: Callable[[str], bool] | None = None) -> ConversationView:
        account, group = turn.account, turn.group
        turn.allowed_sender = allowed_sender

        def current_history():
            messages, senders = self._history(account, group, provider, allowed_sender=allowed_sender)
            # Called immediately before dispatch, after search/account waits.
            turn.history_senders = senders
            return messages

        return ConversationView(self.participant_id(account, group, turn.sender),
            current_history)

    @staticmethod
    def valid(turn: ConversationTurn) -> bool:
        return turn.allowed_sender is None or all(
            turn.allowed_sender(sender) for sender in turn.history_senders | {turn.sender})

    def _prune(self):
        expired = [key for key, session in self._sessions.items()
                   if self._now() - session.updated_at >= CONTEXT_TTL]
        for key in expired:
            del self._sessions[key]
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)

    def history(self, account: str, group: str, provider: object,
                *, allowed_sender: Callable[[str], bool] | None = None) -> list[dict]:
        return self._history(account, group, provider, allowed_sender=allowed_sender)[0]

    def member_questions(self, turn: ConversationTurn, provider: object,
                         *, allowed_sender: Callable[[str], bool]) -> list[str]:
        """Only this known member's own questions, refreshed before dispatch."""
        self._prune()
        if self.participant_id(turn.account, turn.group, turn.sender) == "unknown" or not allowed_sender(turn.sender):
            return []
        session = self._sessions.get((turn.account, turn.group, id(provider)))
        if session is None:
            return []
        questions = [exchange.question[:800] for exchange in session.exchanges
                     if exchange.sender == turn.sender and all(allowed_sender(sender) for sender in exchange.history_senders | {exchange.sender})]
        if questions:
            turn.history_senders = frozenset({turn.sender})
            turn.allowed_sender = allowed_sender
        return questions[-3:]

    def _history(self, account: str, group: str, provider: object,
                 *, allowed_sender: Callable[[str], bool] | None = None) -> tuple[list[dict], frozenset[str]]:
        self._prune()
        session = self._sessions.get((account, group, id(provider)))
        if session is None or not account or not group:
            return [], frozenset()
        messages = []
        senders = set()
        for exchange in session.exchanges:
            dependencies = exchange.history_senders | {exchange.sender}
            if allowed_sender is not None and not all(allowed_sender(sender) for sender in dependencies):
                continue
            senders.update(dependencies)
            messages.extend([{"role": "user", "content": exchange.question,
                              "participant": self.participant_id(account, group, exchange.sender)},
                             {"role": "assistant", "content": exchange.answer}])
        # Return fresh message dictionaries, never a shared mutable history list.
        return messages, frozenset(senders)

    def commit(self, turn: ConversationTurn, sent_answer: str):
        if (not turn.generated or turn.provider is None or not turn.account or not turn.group
                or not self.valid(turn)):
            return
        self._prune()
        key = (turn.account, turn.group, id(turn.provider))
        session = self._sessions.get(key) or Session(turn.provider, self._now())
        session.exchanges.append(Exchange(turn.sender, turn.question, sent_answer, turn.history_senders))
        while (len(session.exchanges) > MAX_TURNS
               or sum(len(item.question) + len(item.answer) for item in session.exchanges) > MAX_CONTEXT_CHARS):
            session.exchanges.pop(0)
        session.updated_at = self._now()
        self._sessions[key] = session
        self._sessions.move_to_end(key)
        self._prune()

    def clear_group(self, account: str, group: str):
        for key in list(self._sessions):
            if key[:2] == (account, group):
                del self._sessions[key]

    def clear(self):
        self._sessions.clear()
