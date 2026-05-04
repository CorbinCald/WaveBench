"""Image mode — text-to-image generation via OpenRouter chat completions.

Image models return generated images as base64 data URLs in the assistant
message. This module extracts and validates those URLs, leaving any assistant
text ignored.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from wavebench.modes import ParsedOutput
from wavebench.tui.styles import THEMES

_IMAGE_DATA_URL_RE = re.compile(
    r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)",
    re.IGNORECASE,
)

_EXTENSION_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


@dataclass(frozen=True)
class DecodedImage:
    """A generated image decoded from an assistant-message data URL."""

    data: bytes
    mime_type: str
    extension: str


@dataclass(frozen=True)
class ImageMode:
    """Text-to-image mode using OpenRouter image-output chat models."""

    name: str = "image"
    display_name: str = "Image"
    aspect_ratio: str = "1:1"
    image_size: str = "1K"
    custom_settings: bool = False

    def frame_prompt(self, user_prompt: str) -> str:
        return user_prompt.strip()

    def image_config(self) -> dict[str, str] | None:
        """Return provider image_config only when custom settings are enabled."""
        if not self.custom_settings:
            return None
        return {
            "aspect_ratio": self.aspect_ratio,
            "image_size": self.image_size,
        }

    def parse_response(self, raw: str | bytes) -> ParsedOutput:
        images = extract_image_outputs(raw)
        if not images:
            return ParsedOutput(
                content=b"",
                extension="png",
                parse_ok=False,
                parse_error="no valid base64 image data URLs",
            )
        first = images[0]
        return ParsedOutput(
            content=first.data,
            extension=first.extension,
            parse_ok=True,
        )


def _extension_for_mime(mime_type: str) -> str:
    mime = mime_type.lower()
    if mime in _EXTENSION_BY_MIME:
        return _EXTENSION_BY_MIME[mime]
    subtype = mime.split("/", 1)[-1].split("+", 1)[0]
    subtype = re.sub(r"[^a-z0-9]", "", subtype)
    return subtype or "img"


def _detect_mime_from_bytes(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _decode_data_url(mime_type: str, payload: str) -> DecodedImage | None:
    cleaned = "".join(payload.split())
    try:
        data = base64.b64decode(cleaned, validate=True)
    except Exception:
        return None
    if not data:
        return None
    mime = _detect_mime_from_bytes(data) or mime_type.lower()
    return DecodedImage(data=data, mime_type=mime, extension=_extension_for_mime(mime))


def _extract_from_string(value: str) -> list[DecodedImage]:
    images: list[DecodedImage] = []
    for match in _IMAGE_DATA_URL_RE.finditer(value):
        decoded = _decode_data_url(match.group(1), match.group(2))
        if decoded is not None:
            images.append(decoded)
    return images


def extract_image_outputs(response: Any) -> list[DecodedImage]:
    """Find valid base64 image data URLs anywhere in an assistant message.

    OpenRouter normalizes generated images as data URLs in the assistant
    message. Providers can place them under ``message.images``, inside a
    multimodal ``content`` array, or in text-like fields, so this walks the
    response recursively and ignores all non-image text.
    """
    images: list[DecodedImage] = []

    def add_from_string(text: str) -> None:
        images.extend(_extract_from_string(text))

    def walk(value: Any) -> None:
        if isinstance(value, str):
            add_from_string(value)
        elif isinstance(value, bytes):
            return
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    walk(response)
    return images


def _rgb_csv(color: tuple[int, int, int]) -> str:
    return ", ".join(str(channel) for channel in color)


def _scale_rgb(color: tuple[int, int, int], factor: float, floor: int = 0) -> tuple[int, int, int]:
    return (
        max(floor, min(255, int(color[0] * factor))),
        max(floor, min(255, int(color[1] * factor))),
        max(floor, min(255, int(color[2] * factor))),
    )


def _gallery_theme_vars(theme_name: str) -> tuple[str, str]:
    """Return the resolved TUI theme name and CSS variables for the gallery."""
    resolved_name = theme_name if theme_name in THEMES else "default"
    theme = THEMES[resolved_name]
    pulse = theme["pulse"]
    pulse_dim = theme["pulse_dim"]
    border = theme["border"]
    variables = {
        "bg-rgb": _rgb_csv(_scale_rgb(pulse_dim, 0.12, floor=3)),
        "bg-soft-rgb": _rgb_csv(_scale_rgb(border, 0.35, floor=8)),
        "panel-rgb": _rgb_csv(_scale_rgb(border, 0.55, floor=12)),
        "border-rgb": _rgb_csv(border),
        "accent-rgb": _rgb_csv(pulse[6]),
        "accent-hi-rgb": _rgb_csv(pulse[8]),
        "glow-rgb": _rgb_csv(pulse[3]),
    }
    css = "\n".join(f"      --wb-{key}: {value};" for key, value in variables.items())
    return resolved_name, css


def write_image_gallery(
    output_dir: str,
    prompt: str,
    results: dict[str, Any],
    theme_name: str = "default",
) -> str:
    """Write a combined run-level ``gallery.html`` for successful image outputs."""
    cards: list[str] = []
    gallery_images: list[dict[str, str]] = []
    successful_models = 0
    failed_models = 0
    image_count = 0

    for model_name, info in results.items():
        if info.get("status") != "success":
            failed_models += 1
            continue

        successful_models += 1
        files = info.get("images") or ([info.get("file")] if info.get("file") else [])
        valid_files = [str(filename) for filename in files if filename]
        image_count += len(valid_files)

        for idx, filename in enumerate(valid_files, 1):
            suffix = f" #{idx}" if len(valid_files) > 1 else ""
            label = f"{model_name}{suffix}"
            gallery_index = len(gallery_images)
            gallery_images.append(
                {
                    "src": filename,
                    "label": label,
                    "filename": filename,
                }
            )
            rel = html.escape(filename, quote=True)
            model_label = html.escape(str(model_name), quote=False)
            model_alt = html.escape(str(model_name), quote=True)
            filename_label = html.escape(filename, quote=False)
            suffix_label = html.escape(suffix, quote=False)
            cards.append(
                "<figure class=\"card\">"
                f"<button class=\"preview\" type=\"button\" data-gallery-index=\"{gallery_index}\" "
                f"aria-label=\"Open preview for {model_alt}{html.escape(suffix, quote=True)}\">"
                f"<img src=\"{rel}\" alt=\"{model_alt}{html.escape(suffix, quote=True)}\" "
                "loading=\"lazy\" decoding=\"async\">"
                "</button>"
                "<figcaption>"
                f"<strong>{model_label}{suffix_label}</strong>"
                f"<span class=\"filename\">{filename_label}</span>"
                "<span class=\"actions\">"
                f"<a href=\"{rel}\" target=\"_blank\" rel=\"noopener\">Open full size</a>"
                f"<a href=\"{rel}\" download>Download</a>"
                "</span>"
                "</figcaption>"
                "</figure>"
            )

    body = "\n".join(cards) or '<p class="empty">No images generated.</p>'
    prompt_html = html.escape(prompt, quote=False)
    stats = [
        f"{image_count} image{'s' if image_count != 1 else ''}",
        f"{successful_models} successful model{'s' if successful_models != 1 else ''}",
    ]
    if failed_models:
        stats.append(f"{failed_models} failed model{'s' if failed_models != 1 else ''}")
    stats_html = "".join(f'<span class="stat">{html.escape(stat)}</span>' for stat in stats)
    resolved_theme, theme_vars = _gallery_theme_vars(theme_name)
    images_json = json.dumps(gallery_images, ensure_ascii=False).replace("</", "<\\/")
    content = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WaveBench Image Gallery</title>
  <style>
    :root {
      color-scheme: dark;
__THEME_VARS__
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: rgb(var(--wb-bg-rgb)); color: #eef2ff; }
    body.modal-open { overflow: hidden; }
    body::before { content: ""; position: fixed; inset: 0; z-index: -1; background: radial-gradient(circle at top left, rgba(var(--wb-glow-rgb), 0.28), transparent 34rem), radial-gradient(circle at top right, rgba(var(--wb-accent-rgb), 0.18), transparent 30rem); }
    header { padding: 2rem; border-bottom: 1px solid rgb(var(--wb-border-rgb)); background: linear-gradient(135deg, rgba(var(--wb-panel-rgb), 0.96), rgba(var(--wb-bg-soft-rgb), 0.86)); }
    .shell { max-width: 1180px; margin: 0 auto; }
    h1 { margin: 0 0 0.75rem; font-size: clamp(1.8rem, 3vw, 2.6rem); letter-spacing: -0.04em; }
    .stats { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0 0 1rem; }
    .stat { display: inline-flex; align-items: center; border: 1px solid rgba(var(--wb-border-rgb), 0.9); border-radius: 999px; padding: 0.3rem 0.65rem; background: rgba(var(--wb-bg-soft-rgb), 0.86); color: #d1d5db; font-size: 0.88rem; }
    .prompt-card { margin: 0; border: 1px solid rgba(var(--wb-border-rgb), 0.95); border-radius: 16px; padding: 1rem; background: rgba(var(--wb-panel-rgb), 0.74); }
    .prompt-label { display: block; margin-bottom: 0.35rem; color: rgb(var(--wb-accent-hi-rgb)); font-size: 0.75rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }
    .prompt { margin: 0; color: #e5e7eb; line-height: 1.55; white-space: pre-wrap; }
    main { padding: 2rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1.25rem; }
    .card { margin: 0; border: 1px solid rgba(var(--wb-border-rgb), 0.95); border-radius: 18px; overflow: hidden; background: rgba(var(--wb-panel-rgb), 0.92); box-shadow: 0 20px 48px rgba(0, 0, 0, 0.34); }
    .preview { display: block; width: 100%; padding: 0; border: 0; background: rgb(var(--wb-bg-rgb)); cursor: zoom-in; }
    .preview:focus-visible { outline: 3px solid rgb(var(--wb-accent-hi-rgb)); outline-offset: -3px; }
    .card img { display: block; width: 100%; height: auto; }
    figcaption { display: grid; gap: 0.45rem; padding: 0.9rem 1rem 1rem; color: #e5e7eb; font-size: 0.95rem; }
    .filename { color: #a1a1aa; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.82rem; overflow-wrap: anywhere; }
    .actions { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-top: 0.15rem; }
    .actions a, .modal-link { color: rgb(var(--wb-accent-hi-rgb)); text-decoration: none; font-weight: 600; }
    .actions a:hover, .modal-link:hover { color: rgb(var(--wb-accent-rgb)); text-decoration: underline; }
    .empty { margin: 0; border: 1px dashed rgba(var(--wb-border-rgb), 0.95); border-radius: 16px; padding: 2rem; color: #a1a1aa; text-align: center; background: rgba(var(--wb-panel-rgb), 0.55); }
    .modal[hidden] { display: none; }
    .modal { position: fixed; inset: 0; z-index: 10; display: grid; place-items: center; padding: 1rem; background: rgba(0, 0, 0, 0.84); backdrop-filter: blur(6px); }
    .modal-frame { position: relative; display: grid; grid-template-rows: minmax(0, 1fr) auto; gap: 0.9rem; width: min(96vw, 1200px); max-height: 94vh; border: 1px solid rgb(var(--wb-border-rgb)); border-radius: 22px; padding: 1rem; background: rgba(var(--wb-panel-rgb), 0.96); box-shadow: 0 28px 80px rgba(0, 0, 0, 0.55); }
    .modal-image-wrap { display: grid; place-items: center; min-height: 0; }
    .modal-image { display: block; max-width: 100%; max-height: 76vh; border-radius: 14px; background: rgb(var(--wb-bg-rgb)); object-fit: contain; }
    .modal-caption { display: grid; gap: 0.35rem; padding: 0 0.25rem; }
    .modal-title { color: #f8fafc; font-weight: 700; }
    .modal-filename { color: #a1a1aa; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.86rem; overflow-wrap: anywhere; }
    .modal-actions { display: flex; flex-wrap: wrap; gap: 0.8rem; }
    .modal-close, .modal-nav { border: 1px solid rgba(var(--wb-border-rgb), 0.95); color: #f8fafc; background: rgba(var(--wb-bg-soft-rgb), 0.92); cursor: pointer; }
    .modal-close:hover, .modal-nav:hover, .modal-close:focus-visible, .modal-nav:focus-visible { border-color: rgb(var(--wb-accent-hi-rgb)); color: rgb(var(--wb-accent-hi-rgb)); }
    .modal-close { position: absolute; top: 0.75rem; right: 0.75rem; width: 2.25rem; height: 2.25rem; border-radius: 999px; font-size: 1.35rem; line-height: 1; }
    .modal-nav { position: absolute; top: 50%; transform: translateY(-50%); width: 2.75rem; height: 3.75rem; border-radius: 999px; font-size: 2rem; line-height: 1; }
    .modal-prev { left: 0.75rem; }
    .modal-next { right: 0.75rem; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
    @media (max-width: 640px) { header, main { padding: 1rem; } .grid { grid-template-columns: 1fr; } .modal-frame { padding: 0.75rem; } .modal-nav { width: 2.25rem; height: 3rem; } }
  </style>
</head>
<body data-theme="__THEME_NAME__">
  <header>
    <div class="shell">
      <h1>WaveBench Image Gallery</h1>
      <div class="stats" aria-label="Run summary">__STATS_HTML__</div>
      <section class="prompt-card" aria-label="Prompt">
        <span class="prompt-label">Prompt</span>
        <p class="prompt">__PROMPT_HTML__</p>
      </section>
    </div>
  </header>
  <main><section class="shell grid" aria-label="Generated images">__BODY__</section></main>
  <div class="modal" id="image-modal" role="dialog" aria-modal="true" aria-label="Generated image preview" aria-hidden="true" hidden>
    <div class="modal-frame">
      <button class="modal-close" type="button" data-action="close" aria-label="Close preview">×</button>
      <button class="modal-nav modal-prev" type="button" data-action="prev" aria-label="Previous image">‹</button>
      <button class="modal-nav modal-next" type="button" data-action="next" aria-label="Next image">›</button>
      <div class="modal-image-wrap"><img class="modal-image" id="modal-image" alt=""></div>
      <div class="modal-caption">
        <span class="modal-title" id="modal-title"></span>
        <span class="modal-filename" id="modal-filename"></span>
        <span class="modal-actions">
          <a class="modal-link" id="modal-open" target="_blank" rel="noopener">Open full size</a>
          <a class="modal-link" id="modal-download" download>Download</a>
        </span>
      </div>
    </div>
  </div>
  <script>
    (() => {
      const images = __IMAGES_JSON__;
      const modal = document.getElementById('image-modal');
      const modalImage = document.getElementById('modal-image');
      const modalTitle = document.getElementById('modal-title');
      const modalFilename = document.getElementById('modal-filename');
      const modalOpen = document.getElementById('modal-open');
      const modalDownload = document.getElementById('modal-download');
      const closeButton = modal.querySelector('[data-action="close"]');
      let currentIndex = 0;

      function showImage(index) {
        if (!images.length) return;
        currentIndex = (index + images.length) % images.length;
        const image = images[currentIndex];
        modalImage.src = image.src;
        modalImage.alt = image.label;
        modalTitle.textContent = image.label;
        modalFilename.textContent = image.filename;
        modalOpen.href = image.src;
        modalDownload.href = image.src;
        modalDownload.setAttribute('download', image.filename);
      }

      function openModal(index) {
        if (!images.length) return;
        showImage(index);
        modal.hidden = false;
        modal.setAttribute('aria-hidden', 'false');
        document.body.classList.add('modal-open');
        closeButton.focus();
      }

      function closeModal() {
        modal.hidden = true;
        modal.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('modal-open');
      }

      document.querySelectorAll('[data-gallery-index]').forEach((button) => {
        button.addEventListener('click', () => openModal(Number(button.dataset.galleryIndex || 0)));
      });

      modal.querySelector('[data-action="prev"]').addEventListener('click', () => showImage(currentIndex - 1));
      modal.querySelector('[data-action="next"]').addEventListener('click', () => showImage(currentIndex + 1));
      closeButton.addEventListener('click', closeModal);
      modal.addEventListener('click', (event) => {
        if (event.target === modal) closeModal();
      });
      document.addEventListener('keydown', (event) => {
        if (modal.hidden) return;
        if (event.key === 'Escape') {
          event.preventDefault();
          closeModal();
        }
        if (event.key === 'ArrowLeft') {
          event.preventDefault();
          showImage(currentIndex - 1);
        }
        if (event.key === 'ArrowRight') {
          event.preventDefault();
          showImage(currentIndex + 1);
        }
      });
    })();
  </script>
</body>
</html>
"""
    content = (
        content.replace("__THEME_VARS__", theme_vars)
        .replace("__THEME_NAME__", html.escape(resolved_theme, quote=True))
        .replace("__STATS_HTML__", stats_html)
        .replace("__PROMPT_HTML__", prompt_html)
        .replace("__BODY__", body)
        .replace("__IMAGES_JSON__", images_json)
    )
    path = os.path.join(output_dir, "gallery.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


IMAGE_MODE = ImageMode()
