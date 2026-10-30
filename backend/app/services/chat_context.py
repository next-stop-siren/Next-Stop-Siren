"""What one execution sends to the AI: #66's completed pairs within a budget, then the question once (#68).

The unit is characters (`len`, code points), not provider tokens: counting tokens
exactly needs a tokenizer, which is a new runtime dependency that is not approved.
For Korean text a character is at most about one token, so the budget is a
conservative upper bound on the input tokens of one call. It is not the member's
400/100 limit and not the Codyssey weekly allocation; it only keeps one call small.

Over the budget, the oldest completed pairs are left out, whole pairs at a time, and
the kept pairs are always the most recent ones in a row. A question or answer is
never cut. The current question is always sent in full: the API already limits it
to `MAX_QUESTION_LENGTH`, which is below the budget, so nothing is refused here.
Left-out pairs stay in the database and in the history the screen shows.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.integrations.provider import ContextMessage
from app.models.conversation import Message

# Proposed in #68 and pending the team's approval; see docs/04-reference/ai.md.
# It must stay above the API's question limit, which a unit test checks.
CONTEXT_CHAR_BUDGET = 16_000


@dataclass(frozen=True)
class ContextPair:
    question: str
    answer: str

    @property
    def size(self) -> int:
        return len(self.question) + len(self.answer)


@dataclass(frozen=True)
class BuiltContext:
    messages: list[ContextMessage]
    pairs_sent: int
    pairs_left_out: int
    characters: int


def pair_messages(messages: Iterable[Message]) -> list[ContextPair]:
    """Pair the rows of `chat_jobs.context_before()`, which come as Q1·A1·Q2·A2 (#66).

    The order is taken as it is and never sorted again here. Each completed answer is
    paired with the question right before it when it answers that question. A row out
    of that order, such as a question with no answer after it or an answer to another
    question, is left out rather than guessed at.
    """
    pairs: list[ContextPair] = []
    question: Message | None = None
    for message in messages:
        if message.status != "completed" or not message.content:
            question = None
        elif message.role == "user":
            question = message
        elif question is not None and message.reply_to_message_id == question.id:
            pairs.append(ContextPair(question.content or "", message.content))
            question = None
        else:
            question = None
    return pairs


def build_context(pairs: Sequence[ContextPair], question: str, *, budget: int | None = None) -> BuiltContext:
    """The most recent whole pairs that fit in `budget` with the question, then the question."""
    if budget is None:
        budget = CONTEXT_CHAR_BUDGET
    used = len(question)
    kept = 0
    for pair in reversed(pairs):
        if used + pair.size > budget:
            break
        used += pair.size
        kept += 1
    messages: list[ContextMessage] = []
    for pair in pairs[len(pairs) - kept :]:
        messages += [ContextMessage("user", pair.question), ContextMessage("assistant", pair.answer)]
    messages.append(ContextMessage("user", question))
    return BuiltContext(messages, pairs_sent=kept, pairs_left_out=len(pairs) - kept, characters=used)
