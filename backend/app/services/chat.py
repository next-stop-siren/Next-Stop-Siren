"""Business errors and limits shared by the member chat queue and its API.

Questions and retries are no longer answered inside the request: since #53 they are
admitted to the persistent queue of `app.services.chat_queue` and answered by
`app.services.chat_runner`. Refusals are raised as the errors below; the API layer
turns them into HTTP responses.
"""

# The approved S10 limit in docs/04-reference/api.md: one generation may take this long.
ANSWER_TIME_LIMIT_SECONDS = 30
# An answer that failed or whose outcome is unknown may be tried again.
RETRYABLE_STATUSES = frozenset({"failed", "interrupted"})


class ChatError(Exception):
    """A request refused before anything new is stored."""


class ConversationNotFound(ChatError):
    """The conversation does not exist or belongs to another user; the two are not distinguishable."""


class RequestKeyConflict(ChatError):
    """The request key is already stored with a different question."""


class QuestionNotFound(ChatError):
    """The message is not a question of the conversation."""


class RetryNotAllowed(ChatError):
    """The question's latest answer is not failed or interrupted, or an attempt of it already waits or runs."""


class NotLatestQuestion(ChatError):
    """A later question of the conversation has started, so this one may no longer be retried."""
