from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import subprocess, httpx, os, uuid, shutil

app = FastAPI(title="ReelForge FFmpeg Service")

FONT_BOLD    = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
FONT_MONO    = "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"
FONT_SERIF   = "/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf"

TMP_DIR = "/tmp/reelforge"
os.makedirs(TMP_DIR, exist_ok=True)

# ── Template Presets ───────────────────────────────────────────────────────────
TEMPLATES = {
    "milan": {
        "fontfile": FONT_BOLD, "fontsize": 52, "fontcolor": "white",
        "box": True, "boxcolor": "black@0.55", "boxborderw": 14,
        "x": "(w-text_w)/2", "y": "h-160",
    },
    "tokyo": {
        "fontfile": FONT_BOLD, "fontsize": 64, "fontcolor": "#00ff88",
        "box": True, "boxcolor": "black@0.8", "boxborderw": 18,
        "x": "(w-text_w)/2", "y": "80",
    },
    "austin": {
        "fontfile": FONT_REGULAR, "fontsize": 44, "fontcolor": "white",
        "box": False, "shadowcolor": "black@0.9", "shadowx": 3, "shadowy": 3,
        "x": "60", "y": "h-130",
    },
    "dubai": {
        "fontfile": FONT_BOLD, "fontsize": 58, "fontcolor": "#FFD700",
        "box": True, "boxcolor": "black@0.6", "boxborderw": 16,
        "x": "(w-text_w)/2", "y": "(h-text_h)/2",
    },
    "berlin": {
        "fontfile": FONT_MONO, "fontsize": 46, "fontcolor": "white",
        "box": True, "boxcolor": "#0d0d0d@0.9", "boxborderw": 22,
        "x": "60", "y": "80",
    },
    "sydney": {
        "fontfile": FONT_REGULAR, "fontsize": 42, "fontcolor": "white",
        "box": True, "boxcolor": "#0077b6@0.75", "boxborderw": 18,
        "x": "(w-text_w)/2", "y": "h-200",
    },
    "newyork": {
        "fontfile": FONT_BOLD, "fontsize": 72, "fontcolor": "white",
        "box": False, "shadowcolor": "black@0.85", "shadowx": 4, "shadowy": 4,
        "x": "(w-text_w)/2", "y": "h-190",
    },
    "paris": {
        "fontfile": FONT_SERIF, "fontsize": 50, "fontcolor": "#f8e1f4",
        "box": True, "boxcolor": "#2d2d2d@0.75", "boxborderw": 16,
        "x": "(w-text_w)/2", "y": "h-170",
    },
}

CRF_MAP = {"high": 18, "medium": 23}


# ── Schema ─────────────────────────────────────────────────────────────────────
class RenderRequest(BaseModel):
    clips: list[str]                  # public URLs of input clips
    text: Optional[str] = None        # overlay text
    template: Optional[str] = "milan" # template name
    quality: Optional[str] = "high"   # high | medium


# ── Helpers ────────────────────────────────────────────────────────────────────
async def _download(url: str, dest: str):
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.get(url)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)


def _run(cmd: list, label: str):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"{label} failed: {result.stderr[-1000:]}")


def _build_drawtext(text: str, template_name: str) -> str:
    tmpl = TEMPLATES.get(template_name, TEMPLATES["milan"])
    safe = text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace("%", "\\%")
    parts = [
        f"fontfile={tmpl['fontfile']}",
        f"text={safe}",
        f"fontsize={tmpl['fontsize']}",
        f"fontcolor={tmpl['fontcolor']}",
        f"x={tmpl['x']}",
        f"y={tmpl['y']}",
        "line_spacing=8",
    ]
    if tmpl.get("box"):
        parts += [
            "box=1",
            f"boxcolor={tmpl['boxcolor']}",
            f"boxborderw={tmpl['boxborderw']}",
        ]
    if tmpl.get("shadowcolor"):
        parts += [
            f"shadowcolor={tmpl['shadowcolor']}",
            f"shadowx={tmpl.get('shadowx', 2)}",
            f"shadowy={tmpl.get('shadowy', 2)}",
        ]
    return "drawtext=" + ":".join(parts)


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
    return {"templates": list(TEMPLATES.keys())}


@app.post("/render")
async def render(req: RenderRequest):
    if not req.clips:
        raise HTTPException(status_code=400, detail="No clips provided")

    job_id = uuid.uuid4().hex
    job_dir = f"{TMP_DIR}/{job_id}"
    os.makedirs(job_dir)

    try:
        crf = CRF_MAP.get(req.quality or "high", 18)

        # 1. Download clips
        clip_paths = []
        for i, url in enumerate(req.clips):
            dest = f"{job_dir}/clip_{i}.mp4"
            await _download(url, dest)
            clip_paths.append(dest)

        # 2. Normalize each clip → 1080x1920 (Reels / Shorts format)
        normalized = []
        for i, clip in enumerate(clip_paths):
            out = f"{job_dir}/norm_{i}.mp4"
            _run([
                "ffmpeg", "-y", "-i", clip,
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
            ], f"Normalize clip {i}")
            normalized.append(out)

        # 3. Concatenate
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

        # 4. Text overlay (optional)
        output = f"{job_dir}/output.mp4"
        if req.text:
            drawtext = _build_drawtext(req.text, req.template or "milan")
            _run([
                "ffmpeg", "-y", "-i", concat_out,
                "-vf", drawtext,
                "-c:v", "libx264", "-crf", str(crf),
                "-preset", "slow",
                "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                output,
            ], "Text overlay")
        else:
            shutil.copy(concat_out, output)

        # 5. Return rendered video
        return FileResponse(
            output,
            media_type="video/mp4",
            filename=f"reel_{job_id[:8]}.mp4",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
