"""Auto-install subsystem — detects Python dependencies and installs them
into a local venv under the output directory.

Used only when the ``auto_install`` config toggle is on AND the generated
file is a ``.py``. A single shared ``.venv`` is created per output run so
multiple models' outputs share dependencies.

Public API:
  - ``_detect_dependencies(session, api_key, code)`` — async LLM call that
    returns a list of pip package names.
  - ``_ensure_venv(output_dir)`` — creates the venv lazily under a lock.
  - ``_install_packages(venv_python, packages)`` — pip install with known
    fallbacks (e.g., ``pygame`` → ``pygame-ce`` first, then ``pygame``).
  - ``_venv_python_path(output_dir)`` — pure helper returning the expected
    Python path for an output-dir venv on the current platform.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import aiohttp

from wavebench.api import call_model_async

_DEP_DETECT_MODEL = "google/gemini-2.5-flash-lite"

_venv_lock = asyncio.Lock()


async def _detect_dependencies(
    session: aiohttp.ClientSession,
    api_key: str,
    code: str,
) -> list[str]:
    """Use a fast LLM to detect third-party package imports in Python code."""
    prompt = (
        "Analyze this Python code and list any third-party packages (NOT in the "
        "standard library) it imports. Map import names to their pip package names "
        "(e.g. cv2 -> opencv-python, PIL -> Pillow, sklearn -> scikit-learn, "
        "bs4 -> beautifulsoup4, yaml -> pyyaml, dotenv -> python-dotenv).\n"
        'Return ONLY a JSON object: {"packages": ["pkg1", "pkg2"]}\n'
        'If no third-party packages are needed, return: {"packages": []}\n\n'
        f"```python\n{code[:4000]}\n```"
    )
    try:
        raw = await call_model_async(
            session,
            api_key,
            _DEP_DETECT_MODEL,
            prompt,
            reasoning_effort=None,
            max_tokens=256,
        )
        if not raw:
            return []
        # Parse JSON from potentially noisy response
        text = raw.strip()
        # Try to extract JSON object
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            data = json.loads(text[start : end + 1])
            pkgs = data.get("packages", [])
            if isinstance(pkgs, list):
                return [p for p in pkgs if isinstance(p, str) and p.strip()]
    except Exception:
        pass
    return []


async def _ensure_venv(output_dir: str) -> str:
    """Create a shared venv in the output directory. Returns the venv Python path."""
    venv_dir = os.path.join(output_dir, ".venv")
    if sys.platform == "win32":
        venv_python = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        venv_python = os.path.join(venv_dir, "bin", "python")

    if os.path.isfile(venv_python):
        return venv_python

    async with _venv_lock:
        # Double-check after acquiring lock
        if os.path.isfile(venv_python):
            return venv_python
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "venv",
            venv_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"venv creation failed: {stderr.decode(errors='replace')}")
    return venv_python


# Packages that may lack pre-built wheels on newer Python versions.
# Maps the original package to a list of alternatives to try in order.
_PACKAGE_FALLBACKS: dict[str, list[str]] = {
    "pygame": ["pygame-ce", "pygame"],
}


async def _install_packages(venv_python: str, packages: list[str]) -> bool:
    """Install packages into a venv via pip, with fallbacks for known problem packages."""
    if not packages:
        return True
    all_ok = True
    for pkg in packages:
        candidates = _PACKAGE_FALLBACKS.get(pkg, [pkg])
        installed = False
        for candidate in candidates:
            proc = await asyncio.create_subprocess_exec(
                venv_python,
                "-m",
                "pip",
                "install",
                "--quiet",
                candidate,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                installed = True
                break
        if not installed:
            all_ok = False
    return all_ok


def _venv_python_path(output_dir: str) -> str:
    """Return the expected venv Python path for an output directory."""
    venv_dir = os.path.join(output_dir, ".venv")
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")
