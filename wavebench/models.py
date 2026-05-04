"""Default model mapping and ranking algorithm.

Holds the fallback ``MODEL_MAPPING``, ``TTS_MODEL_MAPPING``, and
``IMAGE_MODEL_MAPPING`` used when no persistent selection exists, and
``_model_score()`` — the heuristic that ranks the OpenRouter catalog by
provider tier, pricing, recency, reasoning/tool capability, and context
length. Also exposes ``is_stealth()``, ``is_tts_model()``, and
``is_image_model()`` classifiers plus TTS provider defaults.
"""

import time

MODEL_MAPPING: dict[str, str] = {
    "gemini3_0Pro": "google/gemini-3-pro-preview",
    "kimik2_5": "moonshotai/kimi-k2.5",
    "minimax_m2.5": "minimax/minimax-m2.5",
    "glm5": "z-ai/glm-5",
    "claudeOpus4.6": "anthropic/claude-opus-4.6",
}

TTS_MODEL_MAPPING: dict[str, str] = {
    "gpt4oMiniTTS": "openai/gpt-4o-mini-tts-2025-12-15",
    "gemini3.1FlashTTS": "google/gemini-3.1-flash-tts-preview",
    "voxtralMiniTts2603": "mistralai/voxtral-mini-tts-2603",
    "csm1b": "sesame/csm-1b",
    "zonosV0.1Hybrid": "zyphra/zonos-v0.1-hybrid",
    "zonosV0.1Transformer": "zyphra/zonos-v0.1-transformer",
    "orpheus3b": "canopylabs/orpheus-3b-0.1-ft",
    "kokoro82m": "hexgrad/kokoro-82m",
}

IMAGE_MODEL_MAPPING: dict[str, str] = {
    "gpt5.4Image2": "openai/gpt-5.4-image-2",
    "gemini3.1FlashImage": "google/gemini-3.1-flash-image-preview",
    "riverflowV2Pro": "sourceful/riverflow-v2-pro",
}

_TIER1_PROVIDERS: frozenset[str] = frozenset(
    {
        "anthropic",
        "openai",
        "google",
        "deepseek",
        "x-ai",
        "moonshotai",
        "minimax",
    }
)
_TIER2_PROVIDERS: frozenset[str] = frozenset(
    {
        "z-ai",
        "meta-llama",
        "mistralai",
        "bytedance-seed",
        "microsoft",
        "cohere",
        "qwen",
    }
)

_KNOWN_TTS_MODEL_IDS: frozenset[str] = frozenset(
    {
        "google/gemini-3.1-flash-tts-preview",
        "zyphra/zonos-v0.1-transformer",
        "zyphra/zonos-v0.1-hybrid",
        "sesame/csm-1b",
        "canopylabs/orpheus-3b-0.1-ft",
        "hexgrad/kokoro-82m",
        "mistralai/voxtral-mini-tts-2603",
        "openai/gpt-4o-mini-tts-2025-12-15",
    }
)
_TTS_ID_MARKERS: frozenset[str] = frozenset(
    {"tts", "speech", "voxtral", "zonos", "csm-1b", "orpheus", "kokoro"}
)
_KNOWN_IMAGE_MODEL_IDS: frozenset[str] = frozenset(IMAGE_MODEL_MAPPING.values())
_IMAGE_ID_MARKERS: frozenset[str] = frozenset(
    {
        "image",
        "imagen",
        "flux",
        "riverflow",
        "stable-diffusion",
        "dall-e",
        "seedream",
        "hidream",
        "qwen-image",
    }
)
_DEFAULT_TTS_VOICE_BY_MARKER: tuple[tuple[str, str], ...] = (
    ("google/", "Kore"),
    ("mistralai/voxtral", "en_paul_neutral"),
    ("zyphra/zonos", "american_female"),
    ("sesame/csm", "conversational_a"),
    ("canopylabs/orpheus", "tara"),
    ("hexgrad/kokoro", "af_alloy"),
)
_DEFAULT_TTS_FORMAT_BY_MARKER: tuple[tuple[str, str], ...] = (
    ("google/", "pcm"),
    ("mistralai/voxtral", "mp3"),
    ("zyphra/zonos", "mp3"),
)


def output_modalities_from_metadata(metadata: dict | None) -> list[str]:
    """Return OpenRouter output modalities from catalog metadata when present."""
    if not metadata:
        return []
    mods = metadata.get("__output_modalities") or metadata.get("output_modalities")
    if not mods:
        arch = metadata.get("architecture") or {}
        if isinstance(arch, dict):
            mods = arch.get("output_modalities")
    if not isinstance(mods, list):
        return []
    return [str(m).lower() for m in mods if m]


def is_tts_model(model_id: str) -> bool:
    """Heuristic for user-configured models that target speech synthesis."""
    lower = model_id.lower()
    return lower in _KNOWN_TTS_MODEL_IDS or any(marker in lower for marker in _TTS_ID_MARKERS)


def is_image_model(model_id: str, metadata: dict | None = None) -> bool:
    """True for models that generate image outputs.

    Catalog metadata is authoritative when available. For bundled/manual
    models, fall back to known IDs and conservative image-generation markers.
    """
    out_mods = output_modalities_from_metadata(metadata)
    if out_mods:
        return "image" in out_mods
    lower = model_id.lower()
    return lower in _KNOWN_IMAGE_MODEL_IDS or any(marker in lower for marker in _IMAGE_ID_MARKERS)


def image_modalities_for_model(model_id: str, metadata: dict | None = None) -> list[str]:
    """Return the OpenRouter ``modalities`` value for an image generation model."""
    out_mods = output_modalities_from_metadata(metadata)
    if "image" in out_mods:
        if "text" in out_mods:
            return ["image", "text"]
        return ["image"]
    return ["image", "text"]


def tts_voice_for_model(model_id: str, configured_voice: str) -> str:
    """Return a provider-compatible voice for bundled TTS defaults.

    The app's default voice is OpenAI's ``alloy``. When benchmarking known
    non-OpenAI TTS models, map that default to a provider-supported voice.
    Explicit user voices pass through unchanged.
    """
    if configured_voice != "alloy":
        return configured_voice
    lower = model_id.lower()
    for marker, voice in _DEFAULT_TTS_VOICE_BY_MARKER:
        if marker in lower:
            return voice
    return configured_voice


def tts_response_format_for_model(model_id: str, configured_format: str) -> str:
    """Return a response format accepted by known OpenRouter TTS providers.

    The app's default TTS format is OpenAI's ``mp3``. When benchmarking known
    non-OpenAI TTS models, map that default to a provider-supported format.
    Explicit user formats pass through unchanged.
    """
    if configured_format != "mp3":
        return configured_format
    lower = model_id.lower()
    for marker, response_format in _DEFAULT_TTS_FORMAT_BY_MARKER:
        if marker in lower:
            return response_format
    return configured_format


_OPENROUTER_UTILITY: frozenset[str] = frozenset(
    {
        "openrouter/auto",
        "openrouter/free",
        "openrouter/bodybuilder",
        "openrouter/cinematika-7b",
    }
)


def is_stealth(model_id: str) -> bool:
    """True for stealth/cloaked models published under the openrouter/ namespace."""
    return model_id.startswith("openrouter/") and model_id not in _OPENROUTER_UTILITY


def _model_score(m: dict) -> float:
    """Score a model for popularity ranking (higher = more prominent)."""
    mid = m.get("id", "")
    provider = mid.split("/")[0] if "/" in mid else ""

    score = 0.0

    # Provider tier (stealth models get T1-equivalent boost)
    if provider in _TIER1_PROVIDERS or is_stealth(mid):
        score += 1000
    elif provider in _TIER2_PROVIDERS:
        score += 500

    # Pricing tier — paid models ranked above free; higher price = more
    # capable frontier model, but capped so price doesn't dominate.
    pricing = m.get("pricing", {})
    try:
        pp = float(pricing.get("prompt") or 0)
    except (TypeError, ValueError):
        pp = 0.0
    if pp > 0:
        score += 200 + min(300, pp * 50_000)  # caps at ~$6/M tokens

    # Recency: bonus for new models, penalty for stale ones
    created = m.get("created") or 0
    age_days = max(0, (time.time() - created) / 86400)
    if age_days <= 10:
        score += 150 * (1 - age_days / 10)
    if age_days <= 30:
        score += 200 - age_days * 2.2
    else:
        score -= (age_days - 30) * 3

    # Capability bonuses
    params = m.get("supported_parameters") or []
    if "reasoning" in params:
        score += 80
    if "tools" in params:
        score += 40

    # Context length bonus (log-scale)
    ctx = m.get("context_length") or 0
    if ctx >= 100_000:
        score += 60
    elif ctx >= 32_000:
        score += 30

    return score
