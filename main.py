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
    headers: Optional[dict] = None  # e.g. {"Authorization": "Bearer <token>"}


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


class EffectsConfig(BaseModel):
    """Optional visual effects — all default off. Applied at render time by FFmpeg."""
    color_grade:          Optional[str]  = "none"   # none|warm|cool|moody|bw|vintage|cross_process
    vignette:             Optional[bool] = False
    cinematic_bars:       Optional[bool] = False    # 80px black bars top+bottom
    grain_intensity:      Optional[int]  = 0         # 0=off, 5-40 (c0s value)
    chromatic_aberration: Optional[bool] = False    # rgbashift rh=-4 bh=4
    text_animation:       Optional[str]  = "none"   # none | slide_up
    transition_type:      Optional[str]  = "none"   # none | dissolve | fade | circleopen | pixelize


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
        .replace(",",  "\\,")   # commas break filter chain when inside drawtext text= value
    )



def _build_vf_filters(
    spec: dict,
    main_text: str,
    position: str,
    has_music: bool,
    effects: Optional[EffectsConfig] = None,
    font_size_override: Optional[int] = None,
) -> str:
    """
    Build the full -vf filter chain:
      1. scale/pad to 1080x1920
      2. (optional) color grade — curves/eq/hue
      3. (optional) vignette
      4. (optional) film grain — noise filter
      5. (optional) chromatic aberration — rgbashift
      6. main hook text with stroke + shadow (no background band)
      7. thin accent divider + subtitle text
      8. (optional) cinematic bars — drawbox top+bottom
    """
    font_path  = _resolve_font(spec.get("font", "Oswald"))
    font_size  = spec.get("font_size", 28)
    text_color = spec.get("text_color", "#ffffff")
    accent_col = spec.get("accent_color", "#ffffff")
    subtitle   = spec.get("subtitle", "")

    main_safe = _escape_drawtext(main_text or "")
    sub_safe  = _escape_drawtext(subtitle or "")

    # ── 1. Scale + pad ─────────────────────────────────────────────────────────
    scale_pad = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
        "format=yuv420p"
    )

    VIDEO_H = 1920
    VIDEO_W = 1080

    # ── 2. Color grade ─────────────────────────────────────────────────────────
    # Uses curves (spline colour correction) + eq (brightness/contrast/saturation)
    # and hue (desaturation). All inline — no .cube LUT files needed.
    color_grade_filter = ""
    if effects and effects.color_grade and effects.color_grade != "none":
        grade = effects.color_grade
        if grade == "warm":
            # Boost reds/yellows, reduce blues → golden-hour look
            color_grade_filter = (
                ",curves=r='0/0 0.5/0.57 1/1':g='0/0 0.5/0.52 1/1':b='0/0 0.5/0.42 1/0.88'"
            )
        elif grade == "cool":
            # Lift blues, reduce reds → editorial cold look
            color_grade_filter = (
                ",curves=r='0/0 1/0.88':b='0/0 1/1.10'"
                ",eq=saturation=0.92"
            )
        elif grade == "moody":
            # Lifted blacks, lower saturation, slight contrast boost → dark moody
            color_grade_filter = (
                ",eq=contrast=1.10:saturation=0.72:brightness=-0.06"
            )
        elif grade == "bw":
            # True black-and-white with enhanced contrast
            color_grade_filter = ",hue=s=0,eq=contrast=1.25"
        elif grade == "vintage":
            # FFmpeg built-in vintage preset (lifted shadows, muted colours)
            color_grade_filter = ",curves=preset=vintage"
        elif grade == "cross_process":
            # Cross-processed film look (built-in preset)
            color_grade_filter = ",curves=preset=cross_process"

    # ── 3. Vignette ─────────────────────────────────────────────────────────────
    # mode=forward (default) darkens edges — exactly what we want.
    # angle PI/4 ≈ 45° gives a visible but not extreme vignette.
    vignette_filter = ""
    if effects and effects.vignette:
        vignette_filter = ",vignette=a=PI/4"

    # ── 4. Film grain ──────────────────────────────────────────────────────────
    # noise filter: c0s = luma noise strength (0-100), c0f = t+u means temporal+uniform
    # temporal: grain changes every frame (realistic). uniform: flat distribution.
    grain_filter = ""
    if effects and effects.grain_intensity and effects.grain_intensity > 0:
        strength = max(5, min(int(effects.grain_intensity), 40))
        grain_filter = f",noise=c0s={strength}:c0f=t+u"

    # ── 5. Chromatic aberration ────────────────────────────────────────────────
    # rgbashift: shifts red left (rh=-4) and blue right (bh=4) by 4 pixels.
    # smear edge mode avoids black margins at frame edges.
    # Available since FFmpeg 4.2.
    chroma_filter = ""
    if effects and effects.chromatic_aberration:
        chroma_filter = ",rgbashift=rh=-4:bh=4:edge=smear"

    # ── Font size + vertical placement ────────────────────────────────────────
    # If caller set a manual override, use it directly (clamped to 16-72).
    # Otherwise scale template base size by 1.5× and clamp to 36-46px.
    if font_size_override and font_size_override > 0:
        vid_font_size = max(min(int(font_size_override), 72), 16)
    else:
        vid_font_size = max(min(int(font_size * 1.5), 46), 36)
    sub_size = max(int(vid_font_size * 0.55), 16)  # proportional to main — scales with font size
    text_block_h  = vid_font_size + sub_size + 28

    if position == "top":
        main_y_px = 180   # was 120 — give safe distance from top edge
    elif position == "center":
        main_y_px = (VIDEO_H - text_block_h) // 2
    else:
        main_y_px = VIDEO_H - text_block_h - 280

    div_y_px = main_y_px + vid_font_size + 10
    sub_y_px = main_y_px + vid_font_size + 22

    # ── Overlay band behind text (replaces border-stroke highlight) ────────────
    # Parse the template's overlay color (e.g. "rgba(0,0,0,0.55)") and draw a
    # semi-transparent rectangle behind the text block so the text sits cleanly
    # on a colored band rather than relying on a thick stroke for contrast.
    overlay_str  = spec.get("overlay", "")
    band_filter  = ""
    if overlay_str and overlay_str != "rgba(0,0,0,0)":
        ov_alpha = _parse_overlay_alpha(overlay_str)
        ov_r, ov_g, ov_b = _parse_overlay_rgb(overlay_str)
        if ov_alpha > 0.05:
            band_alpha_hex = f"{min(int(ov_alpha * 255), 255):02x}"
            band_color     = f"#{ov_r:02x}{ov_g:02x}{ov_b:02x}{band_alpha_hex}"
            band_pad       = 24   # px padding around text block
            band_y         = max(0, main_y_px - band_pad)
            band_h         = text_block_h + band_pad * 2
            band_filter    = (
                f",drawbox=x=0:y={band_y}:w={VIDEO_W}:h={band_h}"
                f":color={band_color}:t=fill"
            )

    # ── Readability: stroke + shadow ──────────────────────────────────────────
    # Keep a thin border (1px) for extra sharpness but no thick highlight stroke
    tc_ffmpeg    = _hex_to_rgba(text_color)
    is_dark_text = int(text_color.strip().lstrip("#")[:2] or "ff", 16) < 0x88
    border_color = "white@0.45" if is_dark_text else "black@0.45"
    shadow_color = "white@0.35" if is_dark_text else "black@0.35"

    # ── 6. Main hook text ──────────────────────────────────────────────────────
    use_slide_up = effects and effects.text_animation == "slide_up"
    if use_slide_up:
        slide_dist = VIDEO_H - main_y_px
        y_expr     = f"if(lt(t\\,0.5)\\,{main_y_px}+{slide_dist}*(1-t/0.5)\\,{main_y_px})"
        main_filter = (
            f"drawtext=fontfile='{font_path}'"
            f":text='{main_safe}'"
            f":fontsize={vid_font_size}"
            f":fontcolor={tc_ffmpeg}"
            ":x=(w-text_w)/2"
            f":y={y_expr}"
            f":borderw=1:bordercolor={border_color}"
            f":shadowcolor={shadow_color}"
            ":shadowx=2:shadowy=2"
            ":line_spacing=4"
        )
    else:
        main_filter = (
            f"drawtext=fontfile='{font_path}'"
            f":text='{main_safe}'"
            f":fontsize={vid_font_size}"
            f":fontcolor={tc_ffmpeg}"
            ":x=(w-text_w)/2"
            f":y={main_y_px}"
            f":borderw=1:bordercolor={border_color}"
            f":shadowcolor={shadow_color}"
            ":shadowx=2:shadowy=2"
            ":line_spacing=4"
        )

    # ── 7. Accent divider + subtitle ──────────────────────────────────────────
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

    # ── 8. Cinematic bars ──────────────────────────────────────────────────────
    # 80px black bars top and bottom — stylistic letterbox for vertical video.
    # Applied last so they always overlay text (text stays inside bars-free zone).
    bars_filter = ""
    if effects and effects.cinematic_bars:
        bars_filter = (
            ",drawbox=x=0:y=0:w=1080:h=80:color=black:t=fill"
            ",drawbox=x=0:y=1840:w=1080:h=80:color=black:t=fill"
        )

    return (
        f"{scale_pad}"
        f"{color_grade_filter}"
        f"{vignette_filter}"
        f"{grain_filter}"
        f"{chroma_filter}"
        f"{band_filter}"        # overlay band behind text — drawn before text
        f",{main_filter}"
        f"{extra_filters}"
        f"{bars_filter}"
    )


async def _download(url: str, dest: str, headers: Optional[dict] = None) -> None:
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
        r = await client.get(url, headers=headers or {})
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
    effects_json:  str              = Form("{}"),      # JSON-encoded EffectsConfig dict
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

        # Parse effects_json → EffectsConfig (fail-safe: ignore malformed input)
        import json as _json
        fx: Optional[EffectsConfig] = None
        if effects_json and effects_json.strip() not in ("", "{}"):
            try:
                fx = EffectsConfig(**_json.loads(effects_json))
            except Exception:
                pass

        # Extract transition_type for stitch step
        transition_type = (fx.transition_type or "none") if fx else "none"

        # Stitch clips — apply xfade transitions if requested
        stitched = _normalize_clips_from_paths(
            [(ci.url, None, None) for ci in clip_inputs], job_dir, crf,
            transition_type=transition_type,
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

        output = _apply_overlay(stitched, job_dir, crf, spec, hook_text, text_position, music_path, effects=fx)

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
    clips:           list[Union[ClipInput, str]]
    quality:         Optional[str] = "high"
    transition_type: Optional[str] = "none"   # none | dissolve | fade | circleopen | pixelize


class OverlayRequest(BaseModel):
    video_id:      str
    main_text:     Optional[str] = ""
    text_position: Optional[str] = "bottom"
    template:      Optional[Union[TemplateSpec, str]] = None
    music_url:     Optional[str] = None
    music_headers: Optional[dict] = None
    quality:       Optional[str] = "high"
    # Adaptive colors from image_analyzer
    subtitle:      Optional[str] = None
    text_color:    Optional[str] = None   # e.g. "#111111"
    band_rgba:     Optional[str] = None   # e.g. "rgba(255,255,255,0.55)"
    accent_color:  Optional[str] = None   # e.g. "#333333"
    # Visual effects
    effects:       Optional[EffectsConfig] = None
    # Manual overrides — take priority over template + AI values
    font_size_override: Optional[int] = None   # 16-72px; bypasses template font_size + multiplier


def _get_clip_duration(path: str) -> float:
    """Get video duration in seconds using ffprobe."""
    probe = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(probe.stdout.strip())
    except (ValueError, TypeError):
        return 10.0


def _apply_xfade(normalized: list[str], job_dir: str, crf: int, transition_type: str) -> str:
    """
    Concatenate normalized clips with xfade transitions using filter_complex.
    Each transition is 0.5s. Handles clips with no audio stream (inserts anullsrc).
    """
    XFADE_DUR = 0.5
    n = len(normalized)

    if n == 1:
        return normalized[0]

    durations = [_get_clip_duration(p) for p in normalized]

    # Probe each normalized clip for audio
    has_audio_per_clip = []
    for p in normalized:
        probe = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", p],
            capture_output=True, text=True,
        )
        has_audio_per_clip.append("audio" in probe.stdout)

    any_audio = any(has_audio_per_clip)

    # Build xfade video filter chain
    video_parts: list[str] = []
    current_v = "[0:v]"
    cumulative_offset = 0.0

    for i in range(1, n):
        out_label = f"[xv{i}]" if i < n - 1 else "[outv]"
        cumulative_offset += max(durations[i - 1] - XFADE_DUR, 0.1)
        video_parts.append(
            f"{current_v}[{i}:v]xfade=transition={transition_type}"
            f":duration={XFADE_DUR}:offset={cumulative_offset:.3f}{out_label}"
        )
        current_v = f"[xv{i}]"

    out = f"{job_dir}/stitched.mp4"
    cmd = [FFMPEG_BIN, "-y"]
    for p in normalized:
        cmd += ["-i", p]

    if not any_audio:
        # No audio in any clip — skip audio entirely
        filter_complex = ";".join(video_parts)
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-c:v", "libx264", "-crf", str(crf), "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-an",
            out,
        ]
    else:
        # Build audio — insert anullsrc for mute clips to fill gaps
        anullsrc_parts: list[str] = []
        audio_labels: list[str] = []
        for i, has_a in enumerate(has_audio_per_clip):
            if has_a:
                audio_labels.append(f"[{i}:a]")
            else:
                label = f"[anull{i}]"
                anullsrc_parts.append(f"anullsrc=r=44100:cl=stereo{label}")
                audio_labels.append(label)

        audio_concat = (
            "".join(audio_labels) +
            f"concat=n={n}:v=0:a=1[outa]"
        )
        all_parts = video_parts + anullsrc_parts + [audio_concat]
        filter_complex = ";".join(all_parts)

        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "[outa]",
            "-c:v", "libx264", "-crf", str(crf), "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            out,
        ]
    _run(cmd, "Xfade stitch")
    return out


def _normalize_clips_from_paths(
    clip_paths: list[tuple[str, Optional[float], Optional[float]]],
    job_dir: str,
    crf: int,
    transition_type: str = "none",
) -> str:
    """Normalize already-on-disk clips (no HTTP download). Used by /test-render."""
    normalized: list[str] = []
    for i, (clip, start, end) in enumerate(clip_paths):
        out = f"{job_dir}/norm_{i}.mp4"
        cmd = [FFMPEG_BIN, "-y"]
        if start is not None:
            cmd += ["-ss", str(start)]
        audio_probe = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", clip],
            capture_output=True, text=True,
        )
        clip_has_audio = "audio" in audio_probe.stdout
        cmd += [
            "-i", clip,
            "-vf", (
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
                "format=yuv420p"
            ),
            "-c:v", "libx264", "-crf", str(crf),
            "-preset", "fast",
            "-r", "30",
        ]
        if clip_has_audio:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-an"]
        if end is not None:
            duration = end - (start or 0.0)
            cmd += ["-t", str(max(duration, 0.1))]
        cmd.append(out)
        _run(cmd, f"Normalize clip {i}")
        normalized.append(out)

    # xfade transitions (filter_complex) — requires >=2 clips
    if transition_type and transition_type != "none" and len(normalized) > 1:
        return _apply_xfade(normalized, job_dir, crf, transition_type)

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


def _normalize_clips(clip_inputs: list[ClipInput], job_dir: str, crf: int, transition_type: str = "none") -> str:
    """Download, trim, normalize all clips to 1080x1920 and concatenate. Returns stitched path."""
    clip_paths: list[tuple[str, Optional[float], Optional[float]]] = []
    for i, ci in enumerate(clip_inputs):
        dest = f"{job_dir}/clip_{i}.mp4"
        # sync download — called from async context via run_in_executor or directly
        with httpx.Client(timeout=180, follow_redirects=True) as client:
            r = client.get(ci.url, headers=ci.headers or {})
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
        clip_paths.append((dest, ci.start, ci.end))

    normalized: list[str] = []
    for i, (clip, start, end) in enumerate(clip_paths):
        out = f"{job_dir}/norm_{i}.mp4"
        cmd = [FFMPEG_BIN, "-y"]
        # -ss before -i = fast input seek (accurate enough for short clips)
        if start is not None:
            cmd += ["-ss", str(start)]
        # Probe for audio stream — use -an if clip is silent to avoid encode error
        audio_probe = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", clip],
            capture_output=True, text=True,
        )
        clip_has_audio = "audio" in audio_probe.stdout
        cmd += [
            "-i", clip,
            "-vf", (
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
                "format=yuv420p"
            ),
            "-c:v", "libx264", "-crf", str(crf),
            "-preset", "fast",
            "-r", "30",
        ]
        if clip_has_audio:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-an"]
        # -t as OUTPUT option = encode for this many seconds (unambiguous after fast-seek)
        if end is not None:
            duration = end - (start or 0.0)
            cmd += ["-t", str(max(duration, 0.1))]
        cmd.append(out)
        _run(cmd, f"Normalize clip {i}")
        normalized.append(out)

    # xfade transitions (filter_complex) — requires >=2 clips
    if transition_type and transition_type != "none" and len(normalized) > 1:
        return _apply_xfade(normalized, job_dir, crf, transition_type)

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
                   music_url: Optional[str],
                   effects: Optional[EffectsConfig] = None,
                   music_headers: Optional[dict] = None,
                   font_size_override: Optional[int] = None) -> str:
    """Apply text overlay (and optional music) to stitched video. Returns output path."""
    output = f"{job_dir}/output.mp4"
    vf     = _build_vf_filters(spec, main_text, position, has_music=bool(music_url), effects=effects, font_size_override=font_size_override)

    if music_url:
        music_path = f"{job_dir}/music.mp3"
        with httpx.Client(timeout=180, follow_redirects=True) as client:
            r = client.get(music_url, headers=music_headers or {})
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
            # Clips have original audio: mix with background music at 25% volume.
            # duration=first ties mix length to video audio; dropout_transition=3 fades
            # music gracefully when it ends before video.
            "[1:a]volume=0.25[music];[0:a][music]amix=inputs=2:duration=first:dropout_transition=3[a]"
            if has_audio else
            # Clips have no audio: loop music indefinitely so it always covers the
            # full video length. -shortest stops output at video end, no overrun.
            "[1:a]volume=0.25,aloop=loop=-1:size=2e+09[a]"
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
        # Check if stitched video has an audio stream before using -c:a copy
        probe = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", stitched],
            capture_output=True, text=True,
        )
        has_audio = "audio" in probe.stdout
        cmd = [
            FFMPEG_BIN, "-y", "-i", stitched,
            "-vf", vf,
            "-c:v", "libx264", "-crf", str(crf), "-preset", "slow",
            "-pix_fmt", "yuv420p",
        ]
        if has_audio:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-an"]  # no audio
        cmd.append(output)
        _run(cmd, "Text overlay")

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
        stitched  = _normalize_clips(clip_inputs, job_dir, crf,
                                    transition_type=req.transition_type or "none")
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
                                req.main_text or "", position, req.music_url,
                                effects=req.effects, music_headers=req.music_headers,
                                font_size_override=req.font_size_override)

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





