"""
ReelForge FFmpeg Rendering Service
Accepts full template spec + trimmed clips, renders branded Reels/Shorts.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import time
import uuid
from typing import Optional, Union

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="ReelForge FFmpeg Service")

# In-memory store for stitched videos awaiting overlay
# video_id → {dir, stitched, crf, created_at}
STITCH_STORE: dict[str, dict] = {}
STITCH_TTL_S = 1800  # 30 minutes — auto-expire if /overlay never called

FONT_DIR   = "/usr/local/share/fonts/google"
TMP_DIR    = "/tmp/reelforge"
FFMPEG_BIN  = os.getenv("FFMPEG_BIN",  "ffmpeg")   # override for local macOS: /tmp/ffmpeg
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")  # override for local macOS: /tmp/ffprobe
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
    # ── New fonts for templates 21-28 ─────────────────────────────────────────
    "Great Vibes":         f"{FONT_DIR}/GreatVibes-Regular.ttf",
    "Dancing Script":      f"{FONT_DIR}/DancingScript-Bold.ttf",
    "Italiana":            f"{FONT_DIR}/Italiana-Regular.ttf",
    "Anton":               f"{FONT_DIR}/Anton-Regular.ttf",
    "Pacifico":            f"{FONT_DIR}/Pacifico-Regular.ttf",
    "Unbounded":           f"{FONT_DIR}/Unbounded-Bold.ttf",
    "Noto Serif":          f"{FONT_DIR}/NotoSerif-Italic.ttf",
    # Heritage uses Playfair Display 900 — resolve to nearest available weight
    "Playfair Display Black": f"{FONT_DIR}/PlayfairDisplay-Black.ttf",
}

# Fallback font — checked in order, first one that exists wins
_FALLBACK_CANDIDATES = [
    "/usr/local/share/fonts/google/Oswald-Bold.ttf",                     # EasyPanel (downloaded)
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",      # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",              # Linux (Ubuntu/Debian)
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",                 # macOS
    "/Library/Fonts/Arial Bold.ttf",                                     # macOS (Office)
    "/System/Library/Fonts/Helvetica.ttc",                               # macOS (always present)
    "/System/Library/Fonts/Arial.ttf",                                   # macOS
]
FALLBACK_FONT = next((p for p in _FALLBACK_CANDIDATES if os.path.exists(p)), "")

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
    # ── Templates 21-28 ───────────────────────────────────────────────────────
    "coastal":    {"font": "Great Vibes",        "font_weight": 400, "italic": False, "text_case": "none",  "font_size": 30, "text_color": "#fff8f0", "subtitle": "golden hour",    "accent_color": "#fbbf24", "overlay": "rgba(80,30,0,0.45)"},
    "capri":      {"font": "Dancing Script",     "font_weight": 700, "italic": False, "text_case": "none",  "font_size": 28, "text_color": "#e0f2fe", "subtitle": "la dolce vita",  "accent_color": "#7dd3fc", "overlay": "rgba(5,50,90,0.50)"},
    "heritage":   {"font": "Playfair Display",   "font_weight": 900, "italic": False, "text_case": "upper", "font_size": 20, "text_color": "#d4a96a", "subtitle": "old money",      "accent_color": "#d4a96a", "overlay": "rgba(0,0,0,0.68)"},
    "editorial":  {"font": "Italiana",           "font_weight": 400, "italic": False, "text_case": "none",  "font_size": 32, "text_color": "#ffffff", "subtitle": "fashion week",   "accent_color": "#f3f4f6", "overlay": "rgba(0,0,0,0.62)"},
    "noir":       {"font": "Anton",              "font_weight": 400, "italic": False, "text_case": "upper", "font_size": 28, "text_color": "#ffffff", "subtitle": "cinematic",      "accent_color": "#ffffff", "overlay": "rgba(0,0,0,0.80)"},
    "eden":       {"font": "Pacifico",           "font_weight": 400, "italic": False, "text_case": "none",  "font_size": 22, "text_color": "#dcfce7", "subtitle": "in bloom",       "accent_color": "#4ade80", "overlay": "rgba(5,50,20,0.58)"},
    "monaco":     {"font": "Unbounded",          "font_weight": 700, "italic": False, "text_case": "upper", "font_size": 16, "text_color": "#93c5fd", "subtitle": "prestige",       "accent_color": "#3b82f6", "overlay": "rgba(0,20,60,0.72)"},
    "bloom":      {"font": "Noto Serif",         "font_weight": 400, "italic": True,  "text_case": "lower", "font_size": 28, "text_color": "#fff0f5", "subtitle": "in full bloom",  "accent_color": "#fb7185", "overlay": "rgba(200,50,80,0.35)"},
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
    """Return path to a font file — Google Font first, then system fallback."""
    path = FONT_FILES.get(font_name or "Oswald", FALLBACK_FONT)
    if os.path.exists(path):
        return path
    if FALLBACK_FONT and os.path.exists(FALLBACK_FONT):
        return FALLBACK_FONT
    raise RuntimeError(
        "No font file found. On macOS run: brew install --cask font-oswald  "
        "or install any TTF font and set FALLBACK_FONT."
    )


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



def _build_vf_filters(
    spec: dict,
    main_text: str,
    position: str,
    has_music: bool,
) -> str:
    """
    Build the full -vf filter chain:
      1. scale/pad to 1080x1920
      2. main hook text with stroke + shadow (no background band)
      3. thin accent divider line
      4. subtitle text (smaller, softer)
    """
    font_path  = _resolve_font(spec.get("font", "Oswald"))
    font_size  = spec.get("font_size", 28)
    text_color = spec.get("text_color", "#ffffff")
    accent_col = spec.get("accent_color", "#ffffff")
    subtitle   = spec.get("subtitle", "")

    # Always sentence case — never ALL CAPS from template
    main_safe = _escape_drawtext(main_text or "")
    sub_safe  = _escape_drawtext(subtitle or "")

    # Scale + pad
    scale_pad = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
        "format=yuv420p"
    )

    VIDEO_H = 1920
    VIDEO_W = 1080

    # ── Font size ─────────────────────────────────────────────────────────────
    # Target 50-58px on 1080px wide — readable but not overwhelming.
    # (Research: 45-65px optimal for fashion reels, 10 words max per line)
    vid_font_size = max(min(int(font_size * 1.85), 58), 48)

    # ── Vertical placement ────────────────────────────────────────────────────
    # Bottom safe zone: 280px from bottom (clears Instagram action bar + likes/comments).
    # Top safe zone: 120px (clears profile row).
    sub_size     = max(int(vid_font_size * 0.50), 26)
    text_block_h = vid_font_size + sub_size + 28   # hook + gap + subtitle

    if position == "top":
        main_y_px = 120
    elif position == "center":
        main_y_px = (VIDEO_H - text_block_h) // 2
    else:  # bottom — 280px clear from very bottom edge
        main_y_px = VIDEO_H - text_block_h - 280

    div_y_px = main_y_px + vid_font_size + 10
    sub_y_px = main_y_px + vid_font_size + 22

    # ── Readability: stroke + shadow (no band) ────────────────────────────────
    # Pro fashion brands use text stroke + drop shadow for clean floating text.
    # Opposite-colour stroke ensures legibility on any background.
    tc_ffmpeg = _hex_to_rgba(text_color)
    is_dark_text = int(text_color.strip().lstrip("#")[:2] or "ff", 16) < 0x88
    border_color  = "white@0.65" if is_dark_text else "black@0.65"
    shadow_color  = "white@0.50" if is_dark_text else "black@0.50"

    # Main hook text — stroke + shadow, centered, no background
    main_filter = (
        f"drawtext=fontfile='{font_path}'"
        f":text='{main_safe}'"
        f":fontsize={vid_font_size}"
        f":fontcolor={tc_ffmpeg}"
        ":x=(w-text_w)/2"
        f":y={main_y_px}"
        f":borderw=2:bordercolor={border_color}"
        f":shadowcolor={shadow_color}"
        ":shadowx=3:shadowy=3"
        ":line_spacing=4"
    )

    # Thin accent divider + subtitle (no background, stroke for readability)
    extra_filters = ""
    if subtitle:
        ac_ffmpeg     = _hex_to_rgba(accent_col)
        ac_ffmpeg_sub = _hex_to_rgba(accent_col, 0.92)
        div_x         = (VIDEO_W - 160) // 2

        extra_filters = (
            f",drawbox=x={div_x}:y={div_y_px}:w=160:h=2:color={ac_ffmpeg}:thickness=fill"
            f",drawtext=fontfile='{font_path}'"
            f":text='{sub_safe}'"
            f":fontsize={sub_size}"
            f":fontcolor={ac_ffmpeg_sub}"
            ":x=(w-text_w)/2"
            f":y={sub_y_px}"
            f":borderw=1:bordercolor={border_color}"
            f":shadowcolor={shadow_color}"
            ":shadowx=2:shadowy=2"
            ":line_spacing=4"
        )

    return f"{scale_pad},{main_filter}{extra_filters}"


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
    r = subprocess.run([FFMPEG_BIN, "-version"], capture_output=True, text=True)
    return {"version": r.stdout.split("\n")[0]}


@app.get("/templates")
def list_templates():
    return {"templates": list(LEGACY_TEMPLATES.keys())}


# ── Frame extraction endpoint (used by /reelforge/local-test on backend) ─────────

@app.post("/frame")
async def extract_frame_endpoint(
    clips: list[UploadFile] = File(...),
):
    """Save uploaded clips, stitch them, extract mid-point frame. Returns {frame_b64}."""
    if not clips:
        raise HTTPException(status_code=400, detail="At least one clip is required.")

    job_id  = uuid.uuid4().hex
    job_dir = f"{TMP_DIR}/{job_id}"
    os.makedirs(job_dir)

    try:
        clip_inputs = []
        for i, upload in enumerate(clips):
            ext  = os.path.splitext(upload.filename or "clip")[1] or ".mp4"
            dest = f"{job_dir}/upload_{i}{ext}"
            with open(dest, "wb") as f:
                f.write(await upload.read())
            clip_inputs.append(dest)

        stitched  = _normalize_clips_from_paths([(p, None, None) for p in clip_inputs], job_dir, CRF_MAP["high"])
        frame_b64 = _extract_frame(stitched, job_dir)

        return {"frame_b64": frame_b64}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        import threading
        threading.Timer(30, lambda: shutil.rmtree(job_dir, ignore_errors=True)).start()


# ── Local / EasyPanel test endpoint (no Google Drive needed) ─────────────────────

@app.post("/test-render")
async def test_render(
    background_tasks: BackgroundTasks,
    clips:         list[UploadFile] = File(...),
    hook_text:     str              = Form("Elevate Your Style"),
    subtitle:      str              = Form(""),        # AI-generated subtitle overrides template default
    template:      str              = Form("milan"),
    text_position: str              = Form("bottom"),
    quality:       str              = Form("high"),
    # Adaptive colors from image_analyzer (optional — override template colors)
    text_color:    str              = Form(""),        # e.g. "#111111" for bright footage
    band_rgba:     str              = Form(""),        # e.g. "rgba(255,255,255,0.55)"
    accent_color:  str              = Form(""),        # e.g. "#333333"
    music:         Optional[UploadFile] = File(None),
):
    """
    All-in-one test endpoint — upload clips directly, get back a rendered MP4.
    No Google Drive or backend needed. Perfect for testing on EasyPanel directly.

    curl example (1 clip):
        curl -X POST https://<easypanel-host>/test-render \\
          -F "clips=@clip1.mp4" \\
          -F "hook_text=DROP NOW" \\
          -F "template=tokyo" \\
          -F "text_position=bottom" \\
          -F "quality=high" \\
          --output result.mp4

    curl example (2 clips + music):
        curl -X POST https://<easypanel-host>/test-render \\
          -F "clips=@clip1.mp4" \\
          -F "clips=@clip2.mp4" \\
          -F "hook_text=RISE AND SHINE" \\
          -F "template=milan" \\
          -F "music=@bg.mp3" \\
          --output result.mp4
    """
    if not clips:
        raise HTTPException(status_code=400, detail="At least one clip is required.")
    if len(clips) > 4:
        raise HTTPException(status_code=400, detail="Maximum 4 clips.")
    if text_position not in ("top", "center", "bottom"):
        text_position = "bottom"

    crf     = CRF_MAP.get(quality, 18)
    job_id  = uuid.uuid4().hex
    job_dir = f"{TMP_DIR}/{job_id}"
    os.makedirs(job_dir)

    try:
        # Save uploaded clips to disk
        clip_inputs: list[ClipInput] = []
        for i, upload in enumerate(clips):
            ext  = os.path.splitext(upload.filename or "clip")[1] or ".mp4"
            dest = f"{job_dir}/upload_{i}{ext}"
            with open(dest, "wb") as f:
                f.write(await upload.read())
            clip_inputs.append(ClipInput(url=dest))   # local path — no HTTP download needed

        # Save music if provided
        music_path: Optional[str] = None
        if music and music.filename:
            ext        = os.path.splitext(music.filename)[1] or ".mp3"
            music_path = f"{job_dir}/music{ext}"
            with open(music_path, "wb") as f:
                f.write(await music.read())

        # Stitch clips (reuse helper but pass local paths directly)
        stitched = _normalize_clips_from_paths(
            [(ci.url, None, None) for ci in clip_inputs], job_dir, crf
        )
        frame_b64 = _extract_frame(stitched, job_dir)

        # Apply overlay — AI colors + subtitle override the template's hardcoded defaults
        spec = _resolve_template(template)
        overrides: dict = {}
        if subtitle:
            overrides["subtitle"] = subtitle
        if text_color:
            overrides["text_color"] = text_color
        if band_rgba:
            overrides["overlay"] = band_rgba
        if accent_color:
            overrides["accent_color"] = accent_color
        if overrides:
            spec = {**spec, **overrides}
        output = _apply_overlay(stitched, job_dir, crf, spec, hook_text, text_position, music_path)

        background_tasks.add_task(shutil.rmtree, job_dir, True)
        return FileResponse(
            output,
            media_type="video/mp4",
            filename=f"test_reel_{job_id[:8]}.mp4",
            headers={"X-Frame-B64-Length": str(len(frame_b64))},
        )

    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))


class StitchRequest(BaseModel):
    clips:   list[Union[ClipInput, str]]
    quality: Optional[str] = "high"


class OverlayRequest(BaseModel):
    video_id:      str
    main_text:     Optional[str] = ""
    text_position: Optional[str] = "bottom"
    template:      Optional[Union[TemplateSpec, str]] = None
    music_url:     Optional[str] = None
    quality:       Optional[str] = "high"
    # Adaptive colors from image_analyzer
    subtitle:      Optional[str] = None
    text_color:    Optional[str] = None   # e.g. "#111111"
    band_rgba:     Optional[str] = None   # e.g. "rgba(255,255,255,0.55)"
    accent_color:  Optional[str] = None   # e.g. "#333333"


def _normalize_clips_from_paths(
    clip_paths: list[tuple[str, Optional[float], Optional[float]]],
    job_dir: str,
    crf: int,
) -> str:
    """Normalize already-on-disk clips (no HTTP download). Used by /test-render."""
    normalized: list[str] = []
    for i, (clip, start, end) in enumerate(clip_paths):
        out = f"{job_dir}/norm_{i}.mp4"
        cmd = [FFMPEG_BIN, "-y"]
        if start is not None:
            cmd += ["-ss", str(start)]
        if end is not None:
            cmd += ["-to", str(end)]
        cmd += [
            "-i", clip,
            "-vf", (
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
                "format=yuv420p"
            ),
            "-c:v", "libx264", "-crf", str(crf),
            "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            "-r", "30",
            out,
        ]
        _run(cmd, f"Normalize clip {i}")
        normalized.append(out)

    if len(normalized) == 1:
        return normalized[0]

    list_file = f"{job_dir}/concat.txt"
    with open(list_file, "w") as f:
        for p in normalized:
            f.write(f"file '{p}'\n")
    stitched = f"{job_dir}/stitched.mp4"
    _run([
        FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", stitched,
    ], "Concat")
    return stitched


def _normalize_clips(clip_inputs: list[ClipInput], job_dir: str, crf: int) -> str:
    """Download, trim, normalize all clips to 1080x1920 and concatenate. Returns stitched path."""
    clip_paths: list[tuple[str, Optional[float], Optional[float]]] = []
    for i, ci in enumerate(clip_inputs):
        dest = f"{job_dir}/clip_{i}.mp4"
        # sync download — called from async context via run_in_executor or directly
        with httpx.Client(timeout=180, follow_redirects=True) as client:
            r = client.get(ci.url)
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
        clip_paths.append((dest, ci.start, ci.end))

    normalized: list[str] = []
    for i, (clip, start, end) in enumerate(clip_paths):
        out = f"{job_dir}/norm_{i}.mp4"
        cmd = [FFMPEG_BIN, "-y"]
        if start is not None:
            cmd += ["-ss", str(start)]
        if end is not None:
            cmd += ["-to", str(end)]
        cmd += [
            "-i", clip,
            "-vf", (
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
                "format=yuv420p"
            ),
            "-c:v", "libx264", "-crf", str(crf),
            "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            "-r", "30",
            out,
        ]
        _run(cmd, f"Normalize clip {i}")
        normalized.append(out)

    if len(normalized) == 1:
        return normalized[0]

    list_file = f"{job_dir}/concat.txt"
    with open(list_file, "w") as f:
        for p in normalized:
            f.write(f"file '{p}'\n")
    stitched = f"{job_dir}/stitched.mp4"
    _run([
        FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", stitched,
    ], "Concat")
    return stitched


def _extract_frame(stitched: str, job_dir: str) -> str:
    """Extract one frame from the middle of the stitched video. Returns base64 JPEG string."""
    probe = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", stitched],
        capture_output=True, text=True,
    )
    try:
        duration = float(probe.stdout.strip())
    except (ValueError, TypeError):
        duration = 10.0

    seek = max(0.5, duration / 2)
    frame_path = f"{job_dir}/frame.jpg"
    _run([
        FFMPEG_BIN, "-y",
        "-ss", str(seek),
        "-i", stitched,
        "-vframes", "1",
        "-q:v", "2",
        frame_path,
    ], "Extract frame")

    with open(frame_path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _apply_overlay(stitched: str, job_dir: str, crf: int,
                   spec: dict, main_text: str, position: str,
                   music_url: Optional[str]) -> str:
    """Apply text overlay (and optional music) to stitched video. Returns output path."""
    output = f"{job_dir}/output.mp4"
    vf     = _build_vf_filters(spec, main_text, position, has_music=bool(music_url))

    if music_url:
        music_path = f"{job_dir}/music.mp3"
        with httpx.Client(timeout=180, follow_redirects=True) as client:
            r = client.get(music_url)
            r.raise_for_status()
            with open(music_path, "wb") as f:
                f.write(r.content)

        probe = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", stitched],
            capture_output=True, text=True,
        )
        has_audio = "audio" in probe.stdout
        audio_filter = (
            "[1:a]volume=0.25[music];[0:a][music]amix=inputs=2:duration=first:dropout_transition=3[a]"
            if has_audio else
            "[1:a]volume=0.25[a]"
        )
        _run([
            FFMPEG_BIN, "-y",
            "-i", stitched, "-i", music_path,
            "-filter_complex", f"[0:v]{vf}[v];{audio_filter}",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-crf", str(crf), "-preset", "slow",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-shortest", output,
        ], "Overlay + music")
    else:
        _run([
            FFMPEG_BIN, "-y", "-i", stitched,
            "-vf", vf,
            "-c:v", "libx264", "-crf", str(crf), "-preset", "slow",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            output,
        ], "Text overlay")

    return output


def _cleanup_stale_store() -> None:
    """Remove stitch store entries older than TTL."""
    cutoff = time.time() - STITCH_TTL_S
    stale  = [vid for vid, s in STITCH_STORE.items() if s["created_at"] < cutoff]
    for vid in stale:
        entry = STITCH_STORE.pop(vid, None)
        if entry:
            shutil.rmtree(entry["dir"], ignore_errors=True)


@app.post("/stitch")
async def stitch(req: StitchRequest):
    """
    Step 1 of 2: Download + normalize + concatenate clips, extract mid-point frame.
    Returns video_id (reference for /overlay) and frame_b64 (for image_analyzer).
    """
    _cleanup_stale_store()

    if not req.clips:
        raise HTTPException(status_code=400, detail="No clips provided")

    clip_inputs = [ClipInput(url=c) if isinstance(c, str) else c for c in req.clips]
    crf     = CRF_MAP.get(req.quality or "high", 18)
    job_id  = uuid.uuid4().hex
    job_dir = f"{TMP_DIR}/{job_id}"
    os.makedirs(job_dir)

    try:
        stitched  = _normalize_clips(clip_inputs, job_dir, crf)
        frame_b64 = _extract_frame(stitched, job_dir)

        STITCH_STORE[job_id] = {
            "dir":        job_dir,
            "stitched":   stitched,
            "crf":        crf,
            "created_at": time.time(),
        }
        return {"video_id": job_id, "frame_b64": frame_b64}

    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/overlay")
async def overlay(req: OverlayRequest, background_tasks: BackgroundTasks):
    """
    Step 2 of 2: Apply text + template + music to the stitched video from /stitch.
    Returns the final rendered video (mp4).
    """
    _cleanup_stale_store()

    entry = STITCH_STORE.get(req.video_id)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail=f"video_id {req.video_id!r} not found or expired. Call /stitch first.",
        )

    job_dir  = entry["dir"]
    stitched = entry["stitched"]
    crf      = entry["crf"]

    spec     = _resolve_template(req.template)
    position = req.text_position or "bottom"
    if position not in ("top", "center", "bottom"):
        position = "bottom"

    # Apply adaptive color overrides from image_analyzer
    ov_overrides: dict = {}
    if req.subtitle:
        ov_overrides["subtitle"] = req.subtitle
    if req.text_color:
        ov_overrides["text_color"] = req.text_color
    if req.band_rgba:
        ov_overrides["overlay"] = req.band_rgba
    if req.accent_color:
        ov_overrides["accent_color"] = req.accent_color
    if ov_overrides:
        spec = {**spec, **ov_overrides}

    try:
        output = _apply_overlay(stitched, job_dir, crf, spec,
                                req.main_text or "", position, req.music_url)

        STITCH_STORE.pop(req.video_id, None)
        background_tasks.add_task(shutil.rmtree, job_dir, True)
        return FileResponse(output, media_type="video/mp4",
                            filename=f"reel_{req.video_id[:8]}.mp4")

    except HTTPException:
        STITCH_STORE.pop(req.video_id, None)
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        STITCH_STORE.pop(req.video_id, None)
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))


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
            cmd = [FFMPEG_BIN, "-y"]

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
                FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
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
                [FFPROBE_BIN, "-v", "error", "-select_streams", "a",
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
                FFMPEG_BIN, "-y",
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
                FFMPEG_BIN, "-y", "-i", concat_out,
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


