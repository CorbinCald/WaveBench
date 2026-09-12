"""Controller-owned identity around an unchanged generated web app."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape


def _single_line(value: str) -> str:
    return " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())


@dataclass(frozen=True)
class PreviewIdentity:
    name: str
    model_id: str
    slot: int
    run_id: str
    prompt: str

    @property
    def label(self) -> str:
        return f"#{self.slot} · {_single_line(self.name)} · Run {self.run_id[:8]}"

    def page(self, preview: str, attempt: int) -> bytes:
        """Keep app titles/navigation inside a frame; never rewrite model output."""
        prompt = _single_line(self.prompt)
        short_prompt = prompt[:77] + "…" if len(prompt) > 80 else prompt
        title = escape(f"{self.label} · {short_prompt} | WaveBench")
        label = escape(f"#{self.slot} · {_single_line(self.name)}")
        model = escape(_single_line(self.model_id), quote=True)
        run = escape(self.run_id, quote=True)
        source = escape(preview, quote=True)
        prompt = escape(prompt, quote=True)
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>{title}</title>
<style>
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; height: 100%; }}
body {{ display: flex; flex-direction: column; background: #111827; color: #f3f4f6;
       font: 14px/1.4 system-ui, sans-serif; }}
header {{ flex: none; padding: 10px 16px; border-bottom: 1px solid #374151; }}
.identity {{ display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 16px; }}
.brand {{ color: #93c5fd; font-weight: 600; }}
h1 {{ margin: 0; font-size: 16px; overflow-wrap: anywhere; }}
.run {{ color: #d1d5db; }}
a {{ margin-left: auto; color: #bfdbfe; }}
a:focus-visible {{ outline: 2px solid #93c5fd; outline-offset: 3px; }}
p {{ margin: 5px 0 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
     color: #d1d5db; }}
iframe {{ display: block; flex: 1; min-height: 0; width: 100%; border: 0; background: white; }}
</style>
</head>
<body>
<header aria-label="Benchmark identity">
  <div class="identity">
    <span class="brand">WaveBench</span>
    <h1 title="{model}">{label}</h1>
    <span class="run" title="{run}">Run {escape(self.run_id[:8])} · Attempt {attempt}</span>
    <a href="{source}" target="_blank" rel="noopener" title="Open the app without the benchmark header">Open app only ↗</a>
  </div>
  <p title="{prompt}">{prompt}</p>
</header>
<iframe src="{source}" title="{label} generated app" allow="autoplay; clipboard-read; clipboard-write" allowfullscreen></iframe>
</body>
</html>
""".encode()
