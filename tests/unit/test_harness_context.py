import base64
import copy
import json
import random

import pytest

from wavebench.harness.context import clean_summary, compaction_reason, plan_compaction
from wavebench.tokens import prompt_tokens


@pytest.mark.parametrize(
    "size,expected",
    [(239_999, None), (240_000, None), (240_001, "context exceeded 240,000 tokens")],
)
def test_exact_threshold(size, expected):
    assert compaction_reason(size, size, 1_050_000, 16_384) == expected


def test_smaller_model_window_and_output_headroom():
    assert (
        compaction_reason(115_000, 117_000, 128_000, 16_384)
        == "model context window needs headroom"
    )
    assert compaction_reason(100_000, 105_000, 128_000, 16_384) is None


def test_large_output_allowance_does_not_force_early_compaction():
    """Requests clamp output to the window, so a 64k allowance needs only useful headroom."""
    assert compaction_reason(70_000, 72_000, 128_000, 64_000) is None
    assert (
        compaction_reason(108_000, 111_000, 128_000, 64_000)
        == "model context window needs headroom"
    )


def transcript():
    return [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": [{"type": "text", "text": "Original user prompt 🎉"}]},
        {"role": "assistant", "content": "Previous full response"},
        {"role": "user", "content": "A correction"},
        {
            "role": "assistant",
            "content": "Latest FULL response",
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "wb", "arguments": '{"command":"ls"}'},
                },
                {
                    "id": "b",
                    "type": "function",
                    "function": {"name": "wb", "arguments": '{"command":"lint"}'},
                },
            ],
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "signature"}],
            "extra_content": {"google": {"thought_signature": "exact"}},
        },
        {"role": "tool", "tool_call_id": "a", "content": "files"},
        {"role": "tool", "tool_call_id": "b", "content": "lint passed"},
        {"role": "user", "content": "Run 1 failed; repair it"},
    ]


def test_encrypted_reasoning_counts_near_provider_size_not_as_text():
    """xAI returned 296,631 bytes of encrypted reasoning for 63,632 reasoning tokens.

    Tokenized as text it read as ~202k tokens and forced a needless compaction.
    """
    blob = base64.b64encode(random.Random(0).randbytes(222_474)).decode()
    signature = base64.b64encode(random.Random(1).randbytes(3_000)).decode()
    messages = [
        {"role": "user", "content": "Build the level."},
        {
            "role": "assistant",
            "content": "Writing the game.",
            "reasoning_details": [
                {"type": "reasoning.text", "text": "Plan the course.", "signature": signature},
                {"type": "reasoning.encrypted", "data": blob},
            ],
        },
    ]
    before = copy.deepcopy(messages)
    tokens = prompt_tokens(messages, [])
    assert 63_632 < tokens < 80_000
    assert compaction_reason(tokens, tokens, 500_000, 64_000) is None
    assert messages == before


def test_first_user_latest_full_agent_and_parallel_tool_results_preserved_exactly():
    messages = transcript()
    before = copy.deepcopy(messages)
    plan = plan_compaction(messages)
    result = plan.apply("Retain correction and unfinished work")
    assert result[:2] == messages[:2]
    assert result[-4:] == messages[-4:]
    assert plan.middle == messages[2:4]
    assert messages == before
    result[-4]["reasoning_details"][0]["data"] = "changed copy"
    assert messages == before
    assert "A correction" in plan.request()[1]["content"]


@pytest.mark.parametrize("summary", [None, "", "   ", "word " * 9000])
def test_invalid_summary_rejected_without_changing_original(summary):
    messages = transcript()
    before = copy.deepcopy(messages)
    with pytest.raises(ValueError):
        plan_compaction(messages).apply(summary)
    assert messages == before


def test_no_removable_history_fails_without_truncating_protected_messages():
    with pytest.raises(ValueError, match="preserving"):
        plan_compaction(transcript()[:3])


def test_summary_request_deduplicates_readable_reasoning_and_omits_opaque_state():
    messages = transcript()
    messages[2].update(
        reasoning="Keep the correction and validate the existing files.",
        reasoning_details=[
            {
                "type": "reasoning.text",
                "text": "Keep the correction and validate the existing files.",
            },
            {"type": "reasoning.summary", "summary": "Unresolved: fix the launch path."},
            {"type": "reasoning.encrypted", "data": "opaque " * 40_000},
        ],
        extra_content={"google": {"thought_signature": "private provider state"}},
    )
    before = copy.deepcopy(messages)
    plan = plan_compaction(messages)
    request = plan.request()
    evidence = json.loads(request[1]["content"])
    middle = evidence["history_to_summarize"][0]
    assert middle["reasoning"] == (
        "Keep the correction and validate the existing files.\nUnresolved: fix the launch path."
    )
    assert "opaque" not in request[1]["content"]
    assert "thought_signature" not in request[1]["content"]
    assert prompt_tokens(request, []) < 1000
    assert messages == before and plan.middle == before[2:4]
    assert plan.apply("Correction remembered")[-4:] == before[-4:]


@pytest.mark.parametrize(
    "leak",
    [
        ' to=wb  (json)\n{"command":"lint"}',
        '<|channel|>commentary to=functions.wb <|constrain|>json<|message|>{"command":"ls"}<|call|>',
        'commentary to=functions.web_search json\n{"query":"three.js"}',
        '{"command":"read","path":"main.js"}<|call|>',
    ],
)
def test_leaked_tool_call_paragraphs_are_removed_from_summary(leak):
    summary = (
        f"{leak}\n\n## Handoff\n- main.js renders the level.\n\n{leak}\n\nOutstanding: submit."
    )
    cleaned, removed = clean_summary(summary)
    assert removed == 2
    assert cleaned == "## Handoff\n- main.js renders the level.\n\nOutstanding: submit."


@pytest.mark.parametrize(
    "summary",
    [
        "## Handoff\n\n\n- Files: `index.html`.\n  Lint passed.\n\nOutstanding: none.\n",
        "Router maps to=home in app.js; redirect to=login is unchanged.",
        "## Notes\n\n```text\nto=wb stays inside a fenced example\n\n<|call|>\n```\n\nDone.",
    ],
)
def test_ordinary_summaries_are_kept_byte_for_byte(summary):
    assert clean_summary(summary) == (summary, 0)
