"""Guards latest_user_text() (added 2026-09-03, see rag_processor.py's
docstring for the full story of the bug this replaced).

RAGContextProcessor used to read `frame.text` off a raw TranscriptionFrame
— a single, possibly mid-sentence fragment. It now reads back whatever the
user aggregator actually committed to the conversation, by finding the
most recent user-role message in the LLM context. These tests pin that
extraction logic directly, independent of pipecat's frame/processor
machinery, the same way needs_retrieval() is tested standalone.
"""

from app.pipeline.rag_processor import latest_user_text


def test_finds_the_last_user_message():
    messages = [
        {"role": "system", "content": "you are a helpful bot"},
        {"role": "assistant", "content": "hello!"},
        {"role": "user", "content": "what is on page 20?"},
    ]
    assert latest_user_text(messages) == "what is on page 20?"


def test_skips_trailing_assistant_and_tool_messages_to_find_the_real_one():
    # The exact shape seen live: a user message followed by a tool call and
    # its result, with no further user text yet.
    messages = [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": '{"time": "12:00"}'},
    ]
    assert latest_user_text(messages) == "what time is it"


def test_no_user_message_returns_none():
    messages = [{"role": "system", "content": "..."}, {"role": "assistant", "content": "hi"}]
    assert latest_user_text(messages) is None


def test_empty_message_list_returns_none():
    assert latest_user_text([]) is None


def test_list_style_content_is_joined():
    # Some LLM context implementations represent content as typed parts
    # (multimodal-style) rather than a plain string.
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "what does"}, {"type": "text", "text": "the doc say"}]},
    ]
    assert latest_user_text(messages) == "what does the doc say"


def test_empty_list_content_returns_none_not_empty_string():
    messages = [{"role": "user", "content": []}]
    assert latest_user_text(messages) is None
