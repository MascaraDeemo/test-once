#!/usr/bin/env python3
"""Render the README terminal demo as a deterministic animated GIF."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:
    raise SystemExit(
        "Pillow is required only to regenerate the demo: python3 -m pip install Pillow"
    ) from exc


WIDTH = 1200
HEIGHT = 640
BACKGROUND = "#0d1117"
CHROME = "#161b22"
TEXT = "#c9d1d9"
MUTED = "#8b949e"
BLUE = "#58a6ff"
GREEN = "#3fb950"
YELLOW = "#d29922"


def font_path() -> str:
    candidates = (
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/SFNSMonoMedium.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("No supported monospace font was found")


def render_frame(lines: list[tuple[str, str]]) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(font_path(), 25)
    body_font = ImageFont.truetype(font_path(), 25)
    footer_font = ImageFont.truetype(font_path(), 19)

    draw.rounded_rectangle(
        (18, 18, WIDTH - 18, HEIGHT - 18),
        radius=16,
        fill=BACKGROUND,
        outline="#30363d",
        width=2,
    )
    draw.rounded_rectangle(
        (18, 18, WIDTH - 18, 76),
        radius=16,
        fill=CHROME,
    )
    draw.rectangle((18, 58, WIDTH - 18, 76), fill=CHROME)
    for x, color in ((48, "#ff5f56"), (78, "#ffbd2e"), (108, "#27c93f")):
        draw.ellipse((x - 7, 40 - 7, x + 7, 40 + 7), fill=color)
    draw.text(
        (150, 27),
        "Test Once - three Codex sessions, one full-suite process",
        font=title_font,
        fill=TEXT,
    )

    y = 102
    for text, color in lines:
        draw.text((46, y), text, font=body_font, fill=color)
        y += 39

    draw.text(
        (46, HEIGHT - 51),
        "Controlled demo: 1.0s Go test | exact source + command + environment key",
        font=footer_font,
        fill=MUTED,
    )
    return image


def frames() -> tuple[list[Image.Image], list[int]]:
    first = [
        ("$ Codex task A -> go test -count=1 ./...", BLUE),
        ("TEST-ONCE MISS: running this exact suite key once", YELLOW),
        ("ok   example.com/test-once-benchmark   1.00s", TEXT),
        ("TEST-ONCE STORED: PASS", GREEN),
    ]
    second = first + [
        ("", TEXT),
        ("$ Codex task B -> go test -count=1 ./...", BLUE),
        ("TEST-ONCE HIT: PASS  (no test process started)", GREEN),
    ]
    third = second + [
        ("", TEXT),
        ("$ Codex task C -> go test -count=1 ./...", BLUE),
        ("TEST-ONCE HIT: PASS  (no test process started)", GREEN),
    ]
    fourth = [
        ("$ test_once.py stats --suite full-ut", BLUE),
        ("run_requests:          3", TEXT),
        ("executed_runs:         1", TEXT),
        ("cache_hits:            2", TEXT),
        ("cache_hit_rate:        66.7%", GREEN),
        ("runs_avoided:          2", GREEN),
        ("", TEXT),
        ("Only passing results are reusable. No telemetry.", MUTED),
    ]
    return (
        [render_frame(content) for content in (first, second, third, fourth)],
        [2000, 2500, 2500, 3000],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "assets" / "demo.gif",
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    images, durations = frames()
    images[0].save(
        args.output,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
