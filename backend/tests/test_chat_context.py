"""What one execution sends to the AI, without a database (#66, #68).

Messages are unsaved ORM objects with fake IDs and fake text.
"""

import logging

from app.api.schemas import MAX_QUESTION_LENGTH
from app.models.conversation import Message
from app.services.chat_context import CONTEXT_CHAR_BUDGET, ContextPair, build_context, pair_messages


def question(message_id, content, status="completed"):
    return Message(id=message_id, role="user", status=status, content=content)


def answer(message_id, reply_to, content, status="completed"):
    return Message(
        id=message_id,
        role="assistant",
        status=status,
        content=content if status == "completed" else None,
        reply_to_message_id=reply_to,
    )


def roles_and_text(built):
    return [(item.role, item.content) for item in built.messages]


def test_pairs_follow_their_questions_even_when_both_questions_were_stored_first():
    # Q2 was admitted before A1 was stored, so ID order alone would be Q1·Q2·A1·A2.
    messages = [question(1, "Q1"), question(2, "Q2"), answer(3, 1, "A1"), answer(4, 2, "A2")]
    assert pair_messages(messages) == [ContextPair("Q1", "A1"), ContextPair("Q2", "A2")]


def test_a_question_without_a_completed_answer_and_an_answer_without_its_question_are_left_out():
    messages = [
        question(1, "Q1"),
        answer(2, 1, "", status="failed"),
        question(3, "Q2"),
        answer(4, 3, "A2"),
        answer(5, 99, "A?"),
        question(6, "Q3"),
        answer(7, 6, "", status="pending"),
    ]
    assert pair_messages(messages) == [ContextPair("Q2", "A2")]


def test_the_question_is_added_once_after_the_pairs():
    built = build_context([ContextPair("Q1", "A1"), ContextPair("Q2", "A2")], "Q3")
    assert roles_and_text(built) == [
        ("user", "Q1"),
        ("assistant", "A1"),
        ("user", "Q2"),
        ("assistant", "A2"),
        ("user", "Q3"),
    ]
    assert (built.pairs_sent, built.pairs_left_out, built.characters) == (2, 0, 10)


def test_pairs_that_fill_the_budget_exactly_are_all_sent():
    pairs = [ContextPair("a" * 3, "b" * 2), ContextPair("c" * 2, "d" * 3)]
    built = build_context(pairs, "q" * 4, budget=14)
    assert (built.pairs_sent, built.pairs_left_out, built.characters) == (2, 0, 14)


def test_one_character_over_leaves_out_the_oldest_whole_pair():
    pairs = [ContextPair("a" * 3, "b" * 2), ContextPair("c" * 2, "d" * 3)]
    built = build_context(pairs, "q" * 5, budget=14)
    assert roles_and_text(built) == [("user", "cc"), ("assistant", "ddd"), ("user", "qqqqq")]
    assert (built.pairs_sent, built.pairs_left_out, built.characters) == (1, 1, 10)


def test_the_kept_pairs_are_the_most_recent_ones_in_a_row():
    # The newest pair does not fit, so the older small one is not sent around it.
    pairs = [ContextPair("a", "b"), ContextPair("c" * 10, "d" * 10)]
    built = build_context(pairs, "q", budget=15)
    assert roles_and_text(built) == [("user", "q")]
    assert (built.pairs_sent, built.pairs_left_out) == (0, 2)


def test_a_long_history_keeps_its_recent_pairs_under_the_default_budget():
    pairs = [ContextPair(f"Q{number}" + "가" * 900, f"A{number}" + "나" * 900) for number in range(30)]
    built = build_context(pairs, "현재 질문")
    assert built.characters <= CONTEXT_CHAR_BUDGET
    assert built.pairs_sent == 8 and built.pairs_left_out == 22
    assert built.messages[0].content.startswith("Q22") and built.messages[-2].content.startswith("A29")


def test_the_longest_valid_question_always_fits_and_is_never_cut():
    assert MAX_QUESTION_LENGTH < CONTEXT_CHAR_BUDGET
    longest = "가" * MAX_QUESTION_LENGTH
    built = build_context([ContextPair("Q", "A" * CONTEXT_CHAR_BUDGET)], longest)
    assert roles_and_text(built) == [("user", longest)]
    assert built.pairs_left_out == 1


def test_a_question_over_the_budget_is_still_sent_whole_without_history():
    # The API refuses such a question; this only shows that nothing here cuts text.
    built = build_context([ContextPair("Q", "A")], "q" * 20, budget=10)
    assert roles_and_text(built) == [("user", "q" * 20)]


def test_building_the_context_logs_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    build_context([ContextPair("비밀 질문", "비밀 답변")], "현재 질문")
    assert caplog.text == ""
