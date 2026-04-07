"""
ReelForge FFmpeg Rendering Service
Accepts full template spec + trimmed clips, renders branded Reels/Shorts.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from typing import Any, Optional, Union

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="ReelForge FFmpeg Service")

FONT_DIR = "/usr/local/share/fonts/google"
TMP_DIR  = "/tmp/reelforge"
os.makedirs(TMP_DIR, exist_ok=True)

CRF_MAP = {"high": 18, "medium": 23, "low": 28}

# Font file name → path mapping (populated from download_fonts.py output)
FONT_FILES: dict[str, str] = {
    "Oswald":              f"{FONT_DIR}/Oswald-Bold.ttf",
    "Zilla Slab":          f"{FONT_DIR}/ZillaSlab-Bold.ttf",
    "Cormorant Garamond":  f"{FONT_DIR}/CormorantGaramond-Light.ttf",
    "Cinzel":              f"{FONT_DIR}/Cinzel-Bold.ttf",
    "Courier Prime":       f"{FONT_DIR}/CourierPrime-Bold.ttf",
    "Raleway":             f"{FONT_DIR}/Raleway-Thin.ttf",
    "Bodoni Moda":         f"{FONT_DIR}/BodoniModa-Bold.ttf",
    "Libre Baskerville":   f"{FONT_DIR}/LibreBaskerville-Italic.ttf",
    "Bebas Neue":          f"{FONT_DIR}/BebasNeue-Regular.ttf",
    "EB Garamond":         f"{FONT_DIR}/EBGaramond-Regular.ttf",
    "Roboto Slab":         f"{FONT_DIR}/RobotoSlab-Light.ttf",
    "Crimson Text":        f"{FONT_DIR}/CrimsonText-Bold.ttf",
    "Lora":                f"{FONT_DIR}/Lora-SemiBold.ttf",
    "Josefin Sans":        f"{FONT_DIR}/JosefinSans-Thin.ttf",
    "DM Serif Display":    f"{FONT_DIR}/DMSerifDisplay-Regular.ttf",
    "Abril Fatface":       f"{FONT_DIR}/AbrilFatface-Regular.ttf",
    "Cardo":               f"{FONT_DIR}/Cardo-Italic.ttf",
    "Montserrat":          f"{FONT_DIR}/Montserrat-Bold.ttf",
    "Spectral":            f"{FONT_DIR}/Spectral-Light.ttf",
    "Playfair Display":    f"{FONT_DIR}/PlayfairDisplay-BoldItalic.ttf",
}

# Fallback font in case a Google Font didn't download
FALLBACK_FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"

# Legacy template presets (for backward-compat when template is sent as a string ID)
LEGACY_TEMPLATES: dict[str, dict] = {
    "milan":      {"font": "Oswald",            "font_weight": 700, "italic": False, "text_case": "upper", "font_size": 28, "text_color": "#fff",    "subtitle": "fashion week",    "accent_color": "#ffffff", "overlay": "rgba(0,0,0,0.40)"},
    "tokyo":      {"font": "Zilla Slab",         "font_weight": 700, "italic": False, "text_case": "upper", "font_size": 26, "text_color": "#00ff88", "subtitle": "next level",      "accent_color": "#00ff88", "overlay": "rgba(0,0,0,0.75)"},
    "austin":     {"font": "Cormorant Garamond", "font_weight": 300, "italic": False, "text_case": "none",  "font_size": 28, "text_color": "#fff",    "subtitle": "aire pro",        "accent_color": "#e5e7eb", "overlay": "rgba(0,0,0,0.28)"},
    "dubai":      {"font": "Cinzel",             "font_weight": 700, "italic": False, "text_case": "upper", "font_size": 22, "text_color": "#FFD700", "subtitle": "luxury edition",  "accent_color": "#FFD700", "overlay": "rgba(10,8,0,0.60)"},
    "berlin":     {"font": "Courier Prime",      "font_weight": 700, "italic": False, "text_case": "upper", "font_size": 20, "text_color": "#ffffff", "subtitle": "minimal.",        "accent_color": "#9ca3af", "overlay": "rgba(0,0,0,0.80)"},
    "sydney":     {"font": "Raleway",            "font_weight": 100, "italic": False, "text_case": "lower", "font_size": 28, "text_color": "#ffffff", "subtitle": "coastal vibes",   "accent_color": "#38bdf8", "overlay": "rgba(2,78,120,0.62)"},
    "newyork":    {"font": "Bodoni Moda",        "font_weight": 900, "italic": False, "text_case": "lower", "font_size": 22, "text_color": "#ffffff", "subtitle": "narcisa",         "accent_color": "#ffffff", "overlay": "rgba(0,0,0,0.52)"},
    "paris":      {"font": "Libre Baskerville",  "font_weight": 400, "italic": True,  "text_case": "none",  "font_size": 26, "text_color": "#f8e1f4", "subtitle": "joie de vivre",   "accent_color": "#f8e1f4", "overlay": "rgba(50,10,50,0.55)"},
    "barcelona":  {"font": "Bebas Neue",         "font_weight": 400, "italic": False, "text_case": "upper", "font_size": 20, "text_color": "#c7d2fe", "subtitle": "butler",          "accent_color": "#818cf8", "overlay": "rgba(20,18,80,0.65)"},
    "chicago":    {"font": "EB Garamond",        "font_weight": 400, "italic": False, "text_case": "lower", "font_size": 28, "text_color": "#fef3c7", "subtitle": "garamond",        "accent_color": "#fde68a", "overlay": "rgba(0,0,0,0.65)"},
    "rome":       {"font": "Roboto Slab",        "font_weight": 300, "italic": True,  "text_case": "lower", "font_size": 28, "text_color": "#d4a96a", "subtitle": "roboto slab",     "accent_color": "#d4a96a", "overlay": "rgba(60,30,0,0.52)"},
    "dallas":     {"font": "Crimson Text",       "font_weight": 700, "italic": False, "text_case": "upper", "font_size": 26, "text_color": "#ffffff", "subtitle": "crimson text",    "accent_color": "#ef4444", "overlay": "rgba(0,0,0,0.52)"},
    "istanbul":   {"font": "Lora",               "font_weight": 600, "italic": False, "text_case": "upper", "font_size": 19, "text_color": "#fef3c7", "subtitle": "lora",            "accent_color": "#fb923c", "overlay": "rgba(120,40,5,0.60)"},
    "losangeles": {"font": "Josefin Sans",       "font_weight": 100, "italic": False, "text_case": "none",  "font_size": 22, "text_color": "#ffffff", "subtitle": "josefin sans",    "accent_color": "#7dd3fc", "overlay": "rgba(2,60,110,0.50)"},
    "london":     {"font": "DM Serif Display",   "font_weight": 400, "italic": False, "text_case": "none",  "font_size": 28, "text_color": "#ffffff", "subtitle": "dm serif",        "accent_color": "#d1d5db", "overlay": "rgba(20,25,40,0.70)"},
    "madrid":     {"font": "Abril Fatface",      "font_weight": 400, "italic": False, "text_case": "none",  "font_size": 28, "text_color": "#ffffff", "subtitle": "abril fatface",   "accent_color": "#f87171", "overlay": "rgba(130,5,5,0.58)"},
    "amsterdam":  {"font": "Cardo",              "font_weight": 400, "italic": True,  "text_case": "lower", "font_size": 26, "text_color": "#fef9c3", "subtitle": "cardo",           "accent_color": "#86efac", "overlay": "rgba(10,50,20,0.62)"},
    "singapore":  {"font": "Montserrat",         "font_weight": 700, "italic": False, "text_case": "upper", "font_size": 16, "text_color": "#ffffff", "subtitle": "montserrat",      "accent_color": "#e879f9", "overlay": "rgba(80,0,100,0.68)"},
    "mumbai":     {"font": "Spectral",           "font_weight": 300, "italic": True,  "text_case": "none",  "font_size": 28, "text_color": "#FFD700", "subtitle": "spectral",        "accent_color": "#fb923c", "overlay": "rgba(120,60,0,0.54)"},
    "vienna":     {"font": "Playfair Display",   "font_weight": 700, "italic": True,  "text_case": "none",  "font_size": 26, "text_color": "#f8e1f4", "subtitle": "playfair display","accent_color": "#e879f9", "overlay": "rgba(25,5,45,0.68)"},
}


# ── Pydantic models ────────────────────────────────────────────────────────────

class ClipInput(BaseModel):
    url: str
    start: Optional[float] = None   # seconds
    end: Optional[float] = None     # seconds


class TemplateSpec(BaseModel):
    id: Optional[str] = None
    font: Optional[str] = "Oswald"
    font_weight: Optional[int] = 700
    italic: Optional[bool] = False
    text_case: Optional[str] = "upper"    # upper | lower | none
    font_size: Optional[int] = 28
    text_color: Optional[str] = "#ffffff"
    subtitle: Optional[str] = ""
    accent_color: Optional[str] = "#ffffff"
    overlay: Optional[str] = "rgba(0,0,0,0.4)"


class RenderRequest(BaseModel):
    # Clips: accept both new {url,start,end} objects and legacy plain strings
    clips: list[Union[ClipInput, str]]
    # Text: new field is main_text, legacy is text
    main_text: Optional[str] = None
    text: Optional[str] = None          # legacy compat
    # Template: accept full spec dict or legacy string ID
    template: Optional[Union[TemplateSpec, str]] = None
    # Text position: top | center | bottom
    text_position: Optional[str] = "bottom"
    # Music overlay
    music_url: Optional[str] = None
    # Quality
    quality: Optional[str] = "high"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _resolve_template(template: Optional[Union[TemplateSpec, str]]) -> dict:
    """Return template spec dict regardless of whether input is ID string or full spec."""
    if template is None:
        return LEGACY_TEMPLATES["milan"]
    if isinstance(template, str):
        return LEGACY_TEMPLATES.get(template, LEGACY_TEMPLATES["milan"])
    # TemplateSpec object — convert to dict
    return template.model_dump()


def _resolve_font(font_name: str) -> str:
    """Return path to downloaded Google Font, falling back to Liberation."""
    path = FONT_FILES.get(font_name or "Oswald", FALLBACK_FONT)
    if os.path.exists(path):
        return path
    return FALLBACK_FONT


def _hex_to_rgba(hex_color: str, alpha: float = 1.0) -> str:
    """Convert #RRGGBB to FFmpeg 0xRRGGBB@alpha notation."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"0x{r:02X}{g:02X}{b:02X}@{alpha:.2f}"


def _parse_overlay_alpha(overlay_str: str) -> float:
    """Extract alpha from 'rgba(r,g,b,alpha)' string."""
    m = re.search(r"rgba\([^,]+,[^,]+,[^,]+,\s*([\d.]+)\)", overlay_str or "")
    if m:
        return min(float(m.group(1)), 1.0)
    return 0.45


def _parse_overlay_rgb(overlay_str: str) -> tuple[int, int, int]:
    """Extract RGB from 'rgba(r,g,b,alpha)' string."""
    m = re.search(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", overlay_str or "")
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return 0, 0, 0


def _apply_text_case(text: str, case: str) -> str:
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    return text


def _escape_drawtext(text: str) -> str:
    """Escape special characters for FFmpeg drawtext."""
    return (
        text
        .replace("\\", "\\\\")
        .replace("'",  "\\'")
        .replace(":",  "\\:")
        .replace("%",  "\\%")
    )


def _y_expr(position: str, zone: str = "main") -> str:
    """
    Calculate y position for drawtext/drawbox.
    zone: 'main' | 'divider' | 'subtitle'
    All values are concrete pixel offsets — no drawtext variables in drawbox exprs.
    """
    if position == "top":
        if zone == "main":     return "80"
        if zone == "divider":  return "210"
        if zone == "subtitle": return "230"
    if position == "center":
        if zone == "main":     return "(h-text_h)/2-40"
        if zone == "divider":  return "h/2+30"
        if zone == "subtitle": return "h/2+50"
    # default: bottom
    if zone == "main":     return "h-200"
    if zone == "divider":  return "h-118"
    if zone == "subtitle": return "h-100"
    return "h-200"


def _build_vf_filters(
    spec: dict,
    main_text: str,
    position: str,
    has_music: bool,
) -> str:
    """
    Build the full -vf filter chain:
      1. scale/pad to 1080x1920
      2. semi-transparent overlay rectangle
      3. main text (drawtext)
      4. accent divider (drawbox)
      5. subtitle (drawtext)
    """
    font_path  = _resolve_font(spec.get("font", "Oswald"))
    font_size  = spec.get("font_size", 28)
    text_color = spec.get("text_color", "#ffffff")
    accent_col = spec.get("accent_color", "#ffffff")
    subtitle   = spec.get("subtitle", "")
    overlay    = spec.get("overlay", "rgba(0,0,0,0.4)")
    text_case  = spec.get("text_case", "upper")

    main_safe = _escape_drawtext(_apply_text_case(main_text or "", text_case))
    sub_safe  = _escape_drawtext(_apply_text_case(subtitle or "", text_case))

    # Scale + pad (normalize to 1080x1920)
    scale_pad = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
        "format=yuv420p"
    )

    # Overlay tint rectangle
    ov_r, ov_g, ov_b = _parse_overlay_rgb(overlay)
    ov_a = _parse_overlay_alpha(overlay)
    overlay_filter = (
        f"drawbox=x=0:y=0:w=iw:h=ih:"
        f"color=#{ov_r:02X}{ov_g:02X}{ov_b:02X}@{ov_a:.2f}:"
        "thickness=fill"
    )

    # Main text
    tc_ffmpeg   = _hex_to_rgba(text_color)
    main_y      = _y_expr(position, "main")
    main_filter = (
        f"drawtext=fontfile='{font_path}'"
        f":text='{main_safe}'"
        f":fontsize={font_size * 3}"       # scale up: spec font_size is CSS px, video is 1080px wide
        f":fontcolor={tc_ffmpeg}"
        ":x=(w-text_w)/2"
        f":y={main_y}"
        ":shadowcolor=black@0.6"
        ":shadowx=3:shadowy=3"
        ":line_spacing=8"
    )

    # Accent divider + subtitle (only if subtitle text exists)
    extra_filters = ""
    if subtitle:
        ac_ffmpeg  = _hex_to_rgba(accent_col)
        div_y      = _y_expr(position, "divider")
        sub_y      = _y_expr(position, "subtitle")
        sub_size   = max(int(font_size * 1.5), 32)

        extra_filters = (
            f",drawbox=x=(iw-300)/2:y={div_y}:w=300:h=3:color={ac_ffmpeg}:thickness=fill"
            f",drawtext=fontfile='{font_path}'"
            f":text='{sub_safe}'"
            f":fontsize={sub_size}"
            f":fontcolor={ac_ffmpeg}@0.85"
            ":x=(w-text_w)/2"
            f":y={sub_y}"
            ":line_spacing=6"
        )

    return f"{scale_pad},{overlay_filter},{main_filter}{extra_filters}"


async def _download(url: str, dest: str) -> None:
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)


def _run(cmd: list[str], label: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"{label} failed: {result.stderr[-2000:]}",
        )


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "ReelForge FFmpeg"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/ffmpeg-version")
def ffmpeg_version():
    r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    return {"version": r.stdout.split("\n")[0]}


@app.get("/templates")
def list_templates():
    return {"templates": list(LEGACY_TEMPLATES.keys())}


@app.post("/render")
async def render(req: RenderRequest, background_tasks: BackgroundTasks):
    if not req.clips:
        raise HTTPException(status_code=400, detail="No clips provided")

    # Resolve main text (new field takes precedence over legacy)
    main_text = req.main_text or req.text or ""

    # Resolve template spec
    spec = _resolve_template(req.template)

    # Resolve text position
    position = req.text_position or "bottom"
    if position not in ("top", "center", "bottom"):
        position = "bottom"

    crf = CRF_MAP.get(req.quality or "high", 18)

    job_id  = uuid.uuid4().hex
    job_dir = f"{TMP_DIR}/{job_id}"
    os.makedirs(job_dir)

    try:
        # ── 1. Download clips ──────────────────────────────────────────────────
        clip_inputs: list[ClipInput] = []
        for c in req.clips:
            if isinstance(c, str):
                clip_inputs.append(ClipInput(url=c))
            else:
                clip_inputs.append(c)

        clip_paths: list[tuple[str, Optional[float], Optional[float]]] = []
        for i, ci in enumerate(clip_inputs):
            dest = f"{job_dir}/clip_{i}.mp4"
            await _download(ci.url, dest)
            clip_paths.append((dest, ci.start, ci.end))

        # ── 2. Normalize each clip (trim if start/end given) → 1080x1920 ──────
        normalized: list[str] = []
        for i, (clip, start, end) in enumerate(clip_paths):
            out = f"{job_dir}/norm_{i}.mp4"
            cmd = ["ffmpeg", "-y"]

            # Trim inputs
            if start is not None:
                cmd += ["-ss", str(start)]
            if end is not None:
                cmd += ["-to", str(end)]

            cmd += ["-i", clip]
            cmd += [
                "-vf", (
                    "scale=1080:1920:force_original_aspect_ratio=decrease,"
                    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
                    "format=yuv420p"
                ),
                "-c:v", "libx264", "-crf", str(crf),
                "-preset", "slow",
                "-c:a", "aac", "-b:a", "192k",
                "-r", "30",
                out,
            ]
            _run(cmd, f"Normalize clip {i}")
            normalized.append(out)

        # ── 3. Concatenate ─────────────────────────────────────────────────────
        if len(normalized) == 1:
            concat_out = normalized[0]
        else:
            list_file = f"{job_dir}/concat.txt"
            with open(list_file, "w") as f:
                for p in normalized:
                    f.write(f"file '{p}'\n")
            concat_out = f"{job_dir}/concat.mp4"
            _run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", list_file,
                "-c", "copy",
                concat_out,
            ], "Concat")

        # ── 4. Download music (optional) ───────────────────────────────────────
        music_path: Optional[str] = None
        if req.music_url:
            music_path = f"{job_dir}/music.mp3"
            await _download(req.music_url, music_path)

        # ── 5. Text overlay + music mix ────────────────────────────────────────
        output = f"{job_dir}/output.mp4"

        vf = _build_vf_filters(spec, main_text, position, has_music=bool(music_path))

        if music_path:
            # Check if concat_out has an audio stream
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", concat_out],
                capture_output=True, text=True,
            )
            has_audio = "audio" in probe.stdout

            if has_audio:
                audio_filter = (
                    "[1:a]volume=0.25[music];"
                    "[0:a][music]amix=inputs=2:duration=first:dropout_transition=3[a]"
                )
            else:
                # No audio in clips — use music only
                audio_filter = "[1:a]volume=0.25[a]"

            _run([
                "ffmpeg", "-y",
                "-i", concat_out,
                "-i", music_path,
                "-filter_complex",
                f"[0:v]{vf}[v];{audio_filter}",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-crf", str(crf),
                "-preset", "slow",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                output,
            ], "Overlay + music")
        else:
            _run([
                "ffmpeg", "-y", "-i", concat_out,
                "-vf", vf,
                "-c:v", "libx264", "-crf", str(crf),
                "-preset", "slow",
                "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                output,
            ], "Text overlay")

        # ── 6. Return rendered video ───────────────────────────────────────────
        # Cleanup job_dir AFTER the response is fully streamed
        background_tasks.add_task(shutil.rmtree, job_dir, True)
        return FileResponse(
            output,
            media_type="video/mp4",
            filename=f"reel_{job_id[:8]}.mp4",
        )

    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))


