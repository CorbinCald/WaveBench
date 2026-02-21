import re
import json
import aiohttp
from typing import Optional, Dict, Any

from llm_benchmarks.api import call_model_async
from llm_benchmarks.tui.styles import _tri, S

async def get_directory_name(session: aiohttp.ClientSession, api_key: str, prompt: str) -> str:
    """Use a fast model to derive a short directory name from the prompt."""
    model = "google/gemini-2.5-flash-lite"
    naming_prompt = (
        "Generate a short, concise, snake_case directory name (max 3 words) "
        "that summarizes the following prompt. Return ONLY the directory "
        "name, no other text, no markdown formatting.\n\nPrompt: " + prompt
    )

    try:
        name = await call_model_async(
            session, api_key, model, naming_prompt,
            reasoning_effort=None,
            max_tokens=64,
        )
        if name:
            clean = name.strip().replace('`', '').strip()
            clean = "".join(c for c in clean if c.isalnum() or c in "._- ")
            if clean:
                return clean
    except Exception as exc:
        exc_str = str(exc) or exc.__class__.__name__
        print(f"    {_tri} {S.DIM}dir name error: {exc_str}{S.RST}")

    return "benchmark_output"

async def parse_with_gemini(session: aiohttp.ClientSession, api_key: str, model_name: str, content: str) -> Optional[Dict[str, Any]]:
    """Use a fast model to extract code, language, and extension."""
    model = "openai/gpt-oss-120b"
    parsing_prompt = (
        "You are a code extraction engine. Your task is to extract the "
        "executable code from the provided text.\n"
        "1. Identify the programming language.\n"
        "2. Extract the FULL code content without any markdown formatting.\n"
        "3. Determine the correct file extension (e.g., .html, .py, .js).\n"
        "Return a raw JSON object with keys: 'code', 'extension', 'language'. "
        "Do not include any markdown formatting or explanation.\n\n"
        f"Text to parse:\n{content[:50000]}"
    )

    try:
        response = await call_model_async(
            session, api_key, model, parsing_prompt,
            reasoning_effort="minimal",
            temperature=0,
            max_tokens=32768,
        )
        if not response:
            return None

        clean = response.strip()
        if "```" in clean:
            match = re.search(
                r'```(?:json)?\s*(.*?)\s*```', clean, re.DOTALL)
            if match:
                clean = match.group(1)

        return json.loads(clean)
    except Exception as exc:
        exc_str = str(exc) or exc.__class__.__name__
        print(f"    {_tri} {S.DIM}parse error for "
              f"{model_name}: {exc_str}{S.RST}")
        return None
