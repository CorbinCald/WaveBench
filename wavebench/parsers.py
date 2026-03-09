import re
import json
import aiohttp
from typing import Optional, Dict, Any, List, Tuple

from wavebench.api import call_model_async
from wavebench.tui.styles import _tri, S

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

_FENCE_RE = re.compile(
    r"(```|~~~)\s*([^\n`]*)\n(.*?)\n\1",
    re.DOTALL,
)

_LANG_TO_EXT = {
    "python": ".py",
    "py": ".py",
    "javascript": ".js",
    "js": ".js",
    "typescript": ".ts",
    "ts": ".ts",
    "tsx": ".tsx",
    "jsx": ".jsx",
    "html": ".html",
    "css": ".css",
    "json": ".json",
    "yaml": ".yaml",
    "yml": ".yml",
    "markdown": ".md",
    "md": ".md",
    "bash": ".sh",
    "sh": ".sh",
    "shell": ".sh",
    "zsh": ".sh",
    "powershell": ".ps1",
    "ps1": ".ps1",
    "go": ".go",
    "java": ".java",
    "c": ".c",
    "cpp": ".cpp",
    "c++": ".cpp",
    "csharp": ".cs",
    "cs": ".cs",
    "rust": ".rs",
    "rs": ".rs",
    "php": ".php",
    "ruby": ".rb",
    "rb": ".rb",
    "swift": ".swift",
    "kotlin": ".kt",
    "sql": ".sql",
    "r": ".r",
    "xml": ".xml",
}


def _lang_to_extension(language: str) -> str:
    return _LANG_TO_EXT.get(language.lower().strip(), "")


def _extract_json_candidates(text: str) -> List[str]:
    """Collect plausible JSON object candidates from noisy text."""
    clean = text.strip()
    candidates = [clean]

    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", clean, re.DOTALL | re.IGNORECASE)
    if fence_match:
        candidates.append(fence_match.group(1).strip())

    first = clean.find("{")
    last = clean.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(clean[first:last + 1].strip())

    # Preserve insertion order, remove duplicates.
    seen = set()
    ordered = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            ordered.append(candidate)
            seen.add(candidate)
    return ordered


def _parse_json_payload(text: str) -> Optional[Dict[str, Any]]:
    for candidate in _extract_json_candidates(text):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            code = data.get("code")
            if isinstance(code, str) and code.strip():
                return data
    return None


def _parse_code_blocks(text: str) -> List[Tuple[str, str]]:
    blocks: List[Tuple[str, str]] = []
    for match in _FENCE_RE.finditer(text):
        info_raw = (match.group(2) or "").strip()
        code = (match.group(3) or "").strip("\n")
        if not code.strip():
            continue
        lang = info_raw.split()[0].lower() if info_raw else ""
        blocks.append((lang, code))
    return blocks


def _salvage_unclosed_fence(text: str) -> Optional[Tuple[str, str]]:
    """Recover code when an LLM opened a fence but never closed it."""
    m = re.search(r"(```|~~~)\s*([^\n`]*)\n(.*)$", text, re.DOTALL)
    if not m:
        return None
    lang = (m.group(2) or "").split()[0].lower() if (m.group(2) or "").strip() else ""
    code = (m.group(3) or "").strip()
    if not code:
        return None
    return lang, code


def _guess_language_from_code(code: str) -> str:
    stripped = code.lstrip()
    first = stripped.splitlines()[0] if stripped.splitlines() else ""

    if stripped.startswith("#!/") and "python" in stripped[:100].lower():
        return "python"
    if stripped.startswith("#!/") and ("bash" in stripped[:100].lower() or "sh" in stripped[:100].lower()):
        return "bash"
    if "<!doctype html" in stripped.lower() or "<html" in stripped.lower():
        return "html"
    if first.startswith("SELECT ") or "\nSELECT " in code.upper():
        return "sql"
    if re.search(r"^\s*def\s+\w+\(", code, re.MULTILINE) or "import " in code and ":" in code:
        return "python"
    if re.search(r"^\s*function\s+\w+\(", code, re.MULTILINE) or "console.log(" in code:
        return "javascript"
    if "interface " in code or re.search(r":\s*(string|number|boolean)\b", code):
        return "typescript"
    if "fmt.Println(" in code or re.search(r"^\s*package\s+\w+", code, re.MULTILINE):
        return "go"
    if "public static void main" in code or "class " in code and ";" in code:
        return "java"
    if "fn main(" in code or "let mut " in code:
        return "rust"
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            json.loads(stripped)
            return "json"
        except json.JSONDecodeError:
            pass
    return "text"


def _strip_trailing_fence(code: str) -> str:
    """Remove stray trailing markdown fence lines left by malformed outputs."""
    cleaned = code.rstrip()
    while True:
        next_cleaned = re.sub(r"\n[ \t]*(```|~~~)[ \t]*$", "", cleaned)
        if next_cleaned == cleaned:
            break
        cleaned = next_cleaned.rstrip()
    return cleaned


def _build_parse_result(code: str, language_hint: str = "", extension_hint: str = "") -> Dict[str, Any]:
    code = _strip_trailing_fence(code)
    language = (language_hint or "").lower().strip()
    if not language:
        language = _guess_language_from_code(code)
    extension = (extension_hint or "").strip()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    if not extension:
        extension = _lang_to_extension(language)
    return {
        "code": code.rstrip() + "\n",
        "extension": extension,
        "language": language,
    }


async def parse_llm_output(session: aiohttp.ClientSession, api_key: str, model_name: str, content: str) -> Optional[Dict[str, Any]]:
    """Extract code, language, and extension from LLM output locally."""
    # Signature is kept for backward compatibility with existing call sites.
    del session, api_key, model_name
    try:
        if not content or not content.strip():
            return None

        # Stage 1: structured extraction when model returned JSON.
        json_payload = _parse_json_payload(content)
        if json_payload is not None:
            code = str(json_payload.get("code", "")).strip()
            if code:
                lang = str(json_payload.get("language", "")).strip()
                ext = str(json_payload.get("extension", "")).strip()
                return _build_parse_result(code, language_hint=lang, extension_hint=ext)

        # Stage 2: robust fenced-block extraction.
        blocks = _parse_code_blocks(content)
        if blocks:
            # Prefer the largest non-JSON block; many LLMs include JSON metadata + code.
            non_json_blocks = [b for b in blocks if b[0] not in ("json", "")]
            candidate_blocks = non_json_blocks if non_json_blocks else blocks
            lang, code = max(candidate_blocks, key=lambda item: len(item[1].strip()))
            return _build_parse_result(code, language_hint=lang)

        # Stage 3: recover from malformed/unclosed fence.
        salvaged = _salvage_unclosed_fence(content)
        if salvaged:
            lang, code = salvaged
            return _build_parse_result(code, language_hint=lang)

        # Stage 4: final fallback — treat the full response as code-like text.
        return _build_parse_result(content.strip())
    except Exception as exc:
        exc_str = str(exc) or exc.__class__.__name__
        print(f"    {_tri} {S.DIM}local parse error: {exc_str}{S.RST}")
        return None
