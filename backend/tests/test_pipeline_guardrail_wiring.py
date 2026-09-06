"""Task 6.1 — confirms the pipeline actually wires the guardrail pieces in,
not just that they exist and work in isolation.

Source-inspection tests, the same convention test_saga_rollback.py's own
`test_the_caller_speaking_is_what_bounds_a_rollback` uses for exactly the
same reason: run_voice_pipeline needs a live WebRTC connection to actually
execute, so what can be verified without one is that the ASSEMBLY code
really does what its comments claim.
"""

import inspect

from app.pipeline import voice_pipeline


def test_guardrail_rule_is_appended_to_every_bots_prompt():
    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "guardrails.GUARDRAIL_RULE" in source


def test_forbidden_topics_rule_is_appended_based_on_the_bots_own_topics():
    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "guardrails.forbidden_topics_rule(guardrail_topics)" in source


def test_the_guardrail_rule_is_unconditional_not_gated_behind_an_if():
    """Unlike SAGA_RULE/BACKGROUND_TOOL_RULE/PAYMENT_SAFETY_RULE/
    APPROVAL_RULE, which only apply to a bot with the matching tool
    configured, GUARDRAIL_RULE applies to every bot regardless — it should
    appear directly in the prompt-building expression, not behind an
    `if has_something:` guard the way those are."""
    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    rule_line = next(line for line in source.splitlines() if "guardrails.GUARDRAIL_RULE" in line)
    assert "if" not in rule_line, (
        "GUARDRAIL_RULE looks like it is conditionally applied — it must reach every bot"
    )


def test_the_input_monitor_is_in_the_pipeline():
    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "GuardrailInputMonitor(" in source


def test_the_input_monitor_is_unconditional_not_gated_on_bot_id():
    """Unlike RAGContextProcessor, which only runs `if bot_id:`, input
    detection should protect every call — a bot without RAG configured
    still deserves the same audit log."""
    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    # The input monitor must appear in the unconditional pipeline_steps
    # list literal, not inside the `if bot_id:` block that RAGContextProcessor
    # is appended in.
    if_bot_id_block = source.split("if bot_id:")[1].split("pipeline_steps += [")[0]
    assert "GuardrailInputMonitor" not in if_bot_id_block


def test_the_output_filter_is_in_the_pipeline():
    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "GuardrailOutputFilter(" in source


def test_the_output_filter_sits_between_the_llm_and_markdown_stripping():
    """Ordering matters: MarkdownStripper must not run first, or the
    guardrail's own leak-phrase matching would be checking text already
    rewritten for speech rather than the model's actual words."""
    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    llm_index = source.index("pipeline_steps += [\n        llm,")
    guardrail_index = source.index("GuardrailOutputFilter(", llm_index)
    markdown_index = source.index("MarkdownStripper()", llm_index)
    assert guardrail_index < markdown_index, (
        "GuardrailOutputFilter runs after MarkdownStripper — it would be "
        "checking text already rewritten for speech, not the model's actual words"
    )


def test_the_input_monitor_receives_the_bots_own_identity():
    """Without session_id/bot_id/user_id actually threaded through, every
    incident would log as anonymous, and an operator reviewing incidents
    could never tell which bot or customer they belonged to."""
    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    call = source[source.index("GuardrailInputMonitor("):source.index(")", source.index("GuardrailInputMonitor("))]
    assert "session_id=session_id" in call
    assert "bot_id=bot_id" in call
    assert "user_id=user_id" in call
