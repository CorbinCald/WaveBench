"""Shared, model-independent estimates for a validation and submission round trip."""

from wavebench.tokens import PromptEstimate

FINISH_OUTPUT_TOKENS = 4_096
FINISH_WARNING_TOKENS = 512
FINISH_TOOL_TOKENS = 4_096


def finish_tool_tokens(output_chars: int = 16_000) -> int:
    # A practical diagnostic allowance, plus tool IDs and result wrappers. Actual
    # tool results remain intact; an unexpectedly large result is recorded.
    return min(output_chars, FINISH_TOOL_TOKENS) + 1_024


def finish_output_tokens(output_tokens: int) -> int:
    return min(output_tokens, FINISH_OUTPUT_TOKENS)


def input_growth(output_tokens: int, output_chars: int = 16_000) -> int:
    """Allow for the response, tool results, and the next input's estimation margin."""
    return PromptEstimate().bound(output_tokens + finish_tool_tokens(output_chars))


def finish_reserve(input_bound: int, output_tokens: int, output_chars: int = 16_000) -> int:
    """Two inputs and outputs, including the first response in the second input.

    The model can batch final fixes with lint, read its result, then call done
    alone. Warning text is paid for in both inputs. Tool-result growth is an
    estimate, not a promise or permission to exceed the total token budget.
    """
    output_tokens = finish_output_tokens(output_tokens)
    return (
        2 * (input_bound + FINISH_WARNING_TOKENS)
        + 2 * output_tokens
        + input_growth(output_tokens, output_chars)
    )


def finishing_trigger(
    input_bound: int, output_tokens: int, reserve: int, output_chars: int = 16_000
) -> int:
    # After another ordinary response, both finishing inputs will repeat that
    # response and its tool results. Warn before their added cost makes the
    # finishing sequence unaffordable. Compaction uses this same boundary.
    return input_bound + output_tokens + reserve + 2 * input_growth(output_tokens, output_chars)
