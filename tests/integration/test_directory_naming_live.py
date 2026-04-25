"""Live integration test for prompt-derived directory naming.

This test intentionally calls OpenRouter. It is skipped unless
``OPENROUTER_API_KEY`` is available in the environment or local ``.env`` file.
"""

from __future__ import annotations

import re

import aiohttp
import pytest

from wavebench.api import load_api_key
from wavebench.parsers import get_directory_name


@pytest.mark.slow
async def test_get_directory_name_live_openrouter_call_succeeds() -> None:
    api_key = load_api_key()
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY not configured")

    async with aiohttp.ClientSession() as session:
        name = await get_directory_name(
            session,
            api_key,
            "Create a small Python snake game with keyboard controls",
        )

    assert name
    assert name != "benchmark_output"
    assert len(name) <= 80
    assert re.fullmatch(r"[A-Za-z0-9._\- ]+", name)
