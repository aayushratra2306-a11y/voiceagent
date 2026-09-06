"""Task 6.1 — the bot API side: per-bot forbidden topics, validated and
bounded, since they run a check on every sentence of every reply on every
live call.
"""

import pytest

from app.api.bots import MAX_GUARDRAIL_TOPIC_LENGTH, MAX_GUARDRAIL_TOPICS
from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _create(client, token, **fields):
    resp = await client.post("/bots/", json={"name": "Guardrail probe", **fields},
                              headers=auth_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _listed(client, token, bot_id):
    body = (await client.get("/bots/", headers=auth_headers(token))).json()
    return next(b for b in body if b["id"] == bot_id)


async def test_a_new_bot_has_no_forbidden_topics_by_default(client, user_a_token):
    """The universal rules (GUARDRAIL_RULE) apply regardless — this list is
    only for what a specific customer opts INTO."""
    bot_id = await _create(client, user_a_token)
    bot = await _listed(client, user_a_token, bot_id)
    assert bot["guardrail_topics"] == []


async def test_a_customer_can_set_forbidden_topics(client, user_a_token):
    bot_id = await _create(client, user_a_token, guardrail_topics=["layoffs", "CompetitorCo"])
    bot = await _listed(client, user_a_token, bot_id)
    assert bot["guardrail_topics"] == ["layoffs", "CompetitorCo"]


async def test_too_many_topics_is_rejected(client, user_a_token):
    topics = [f"topic-{i}" for i in range(MAX_GUARDRAIL_TOPICS + 1)]
    resp = await client.post(
        "/bots/", json={"name": "Too many topics", "guardrail_topics": topics},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422


async def test_exactly_the_maximum_number_of_topics_is_accepted(client, user_a_token):
    topics = [f"topic-{i}" for i in range(MAX_GUARDRAIL_TOPICS)]
    bot_id = await _create(client, user_a_token, guardrail_topics=topics)
    bot = await _listed(client, user_a_token, bot_id)
    assert len(bot["guardrail_topics"]) == MAX_GUARDRAIL_TOPICS


async def test_an_overly_long_topic_is_rejected(client, user_a_token):
    resp = await client.post(
        "/bots/", json={"name": "Long topic", "guardrail_topics": ["x" * (MAX_GUARDRAIL_TOPIC_LENGTH + 1)]},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422


async def test_a_blank_topic_is_rejected():
    """A blank topic checked as a substring against every sentence would
    match everything — trivially 'in' every string."""
    from app.api.bots import _validate_guardrail_topics

    with pytest.raises(ValueError):
        _validate_guardrail_topics(["layoffs", "  "])


async def test_topics_are_trimmed_of_surrounding_whitespace(client, user_a_token):
    bot_id = await _create(client, user_a_token, guardrail_topics=["  layoffs  "])
    bot = await _listed(client, user_a_token, bot_id)
    assert bot["guardrail_topics"] == ["layoffs"]


async def test_topics_can_be_changed_after_creation(client, user_a_token):
    bot_id = await _create(client, user_a_token, guardrail_topics=["layoffs"])

    resp = await client.patch(
        f"/bots/{bot_id}", json={"guardrail_topics": ["CompetitorCo", "lawsuit"]},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 200

    bot = await _listed(client, user_a_token, bot_id)
    assert bot["guardrail_topics"] == ["CompetitorCo", "lawsuit"]


async def test_topics_can_be_cleared_entirely(client, user_a_token):
    bot_id = await _create(client, user_a_token, guardrail_topics=["layoffs"])

    resp = await client.patch(
        f"/bots/{bot_id}", json={"guardrail_topics": []},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 200

    bot = await _listed(client, user_a_token, bot_id)
    assert bot["guardrail_topics"] == []
