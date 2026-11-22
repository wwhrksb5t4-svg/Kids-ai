"""Assemble narrated 1080p nursery-rhyme videos with FFmpeg."""

import asyncio
import io
import math
import subprocess
import threading
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

from pipeline.images import ensure_distinct_images


SIZE = (1920, 1080)
_REMBG_SESSION = None
_REMBG_LOCK = threading.Lock()


def _font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _shape(text: str) -> str:
    return get_display(arabic_reshaper.reshape(text))


def _text_image(text: str, path: Path, *, card: bool = False) -> None:
    image = Image.new("RGBA", SIZE, (66, 144, 182, 255) if card else (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    face = _font(72 if card else 58)
    lines = text.strip().splitlines()[:2]
    draw.multiline_text(
        (960, 540 if card else 915), "\n".join(_shape(line) for line in lines),
        font=face, fill="white", anchor="mm", align="center", spacing=16,
        stroke_width=7 if not card else 3, stroke_fill="black",
    )
    image.save(path)


def _run(command: list[str], timeout: int = 900) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not run FFmpeg.") from exc
    if result.returncode:
        raise RuntimeError(f"FFmpeg failed: {result.stderr[-500:]}")


def _duration(path: Path) -> float:
    from moviepy import AudioFileClip

    clip = AudioFileClip(str(path))
    try:
        value = float(clip.duration)
    finally:
        clip.close()
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError("Audio has invalid duration.")
    return value


def _separate_character(image_path: Path, background_path: Path, foreground_path: Path) -> None:
    import cv2
    import numpy as np
    from rembg import new_session, remove

    global _REMBG_SESSION
    with _REMBG_LOCK:
        if _REMBG_SESSION is None:
            _REMBG_SESSION = new_session("u2net")

    raw = Path(image_path).read_bytes()
    cutout_bytes = remove(raw, session=_REMBG_SESSION)
    with Image.open(io.BytesIO(raw)) as source, Image.open(io.BytesIO(cutout_bytes)) as cutout:
        original = source.convert("RGB")
        layer = cutout.convert("RGBA")
    if layer.size != original.size:
        layer = layer.resize(original.size, Image.Resampling.LANCZOS)
    alpha = np.asarray(layer.getchannel("A"))
    mask = np.where(alpha > 12, 255, 0).astype("uint8")
    subject_pixels = int(np.count_nonzero(mask))
    if subject_pixels < mask.size // 100 or subject_pixels > mask.size * 0.98:
        raise RuntimeError("rembg could not isolate a clear character from the generated scene.")
    mask = cv2.dilate(mask, np.ones((13, 13), dtype=np.uint8), iterations=1)
    clean_plate = cv2.inpaint(
        np.asarray(original), mask, inpaintRadius=9, flags=cv2.INPAINT_TELEA
    )
    background_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(clean_plate).save(background_path, format="PNG")
    layer.save(foreground_path, format="PNG")


async def make_parallax_clip(
    image: Path,
    destination: Path,
    duration: float,
    *,
    size: tuple[int, int] = SIZE,
    work_dir: Path | None = None,
) -> Path:
    """Create gentle 2.5D camera and character-layer movement from an AI scene image."""
    image, destination = Path(image), Path(destination)
    if not image.is_file():
        raise RuntimeError(f"Scene image is missing: {image.name}")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Parallax duration must be positive.")
    if destination.is_file() and destination.stat().st_size:
        return destination
    directory = Path(work_dir) if work_dir else destination.parent
    directory.mkdir(parents=True, exist_ok=True)
    background = directory / f"{destination.stem}-background.png"
    foreground = directory / f"{destination.stem}-character.png"
    await asyncio.to_thread(_separate_character, image, background, foreground)
    width, height = size
    overscan_width, overscan_height = math.ceil(width * 1.12), math.ceil(height * 1.12)
    frames = max(1, math.ceil(duration * 24))
    command = [
        "ffmpeg", "-v", "error", "-y",
        "-loop", "1", "-framerate", "24", "-i", str(background),
        "-loop", "1", "-framerate", "24", "-i", str(foreground),
        "-filter_complex",
        (
            f"[0:v]scale={overscan_width}:{overscan_height}:force_original_aspect_ratio=increase,"
            f"crop={overscan_width}:{overscan_height},"
            f"zoompan=z='min(1+0.00035*on,1.035)':"
            f"x='iw/2-(iw/zoom/2)+10*sin(on/35)':"
            f"y='ih/2-(ih/zoom/2)+8*cos(on/39)':d=1:s={width}x{height}:fps=24[bg];"
            f"[1:v]scale={width}:{height},format=rgba[character];"
            "[bg][character]overlay=x='10*sin(t*0.8)':y='7*cos(t*0.7)':shortest=1,"
            "format=yuv420p[v]"
        ),
        "-map", "[v]", "-frames:v", str(frames), "-an", "-c:v", "libx264",
        "-preset", "veryfast", "-pix_fmt", "yuv420p", str(destination),
    ]
    await asyncio.to_thread(_run, command)
    return destination


def _render_card(image: Path, output: Path, seconds: float) -> None:
    _run([
        "ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "24", "-i", str(image),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(seconds),
        "-vf", "format=yuv420p,fade=t=in:st=0:d=0.3,fade=t=out:st=1.7:d=0.3",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-ar", "44100",
        "-ac", "2", str(output),
    ])


def _render_scene(
    image: Path, voice: Path, captions: Path, output: Path, duration: float,
    motion: Path | None = None,
) -> None:
    if motion is None or not Path(motion).is_file() or Path(motion).stat().st_size == 0:
        raise ValueError("A scene must have a non-empty 2.5D or AI-generated motion clip.")
    fade_out = max(0, duration - 0.3)
    image_input = ["-stream_loop", "-1", "-i", str(motion)]
    background = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,fps=24"
    )
    filters = (
        f"{background},setsar=1,fade=t=in:st=0:d=0.3,"
        f"fade=t=out:st={fade_out:.3f}:d=0.3[bg];"
        "[bg][2:v]overlay=0:0:shortest=1,format=yuv420p[v]"
    )
    _run(
        ["ffmpeg", "-v", "error", "-y", *image_input, "-i", str(voice),
         "-loop", "1", "-framerate", "24", "-i", str(captions),
         "-filter_complex", filters, "-map", "[v]", "-map", "1:a", "-af", "apad",
         "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
         "-ar", "44100", "-ac", "2", str(output)]
    )


def _thumbnail(image: Path, title: str, destination: Path) -> None:
    with Image.open(image) as source:
        cover = ImageOps.fit(source.convert("RGB"), (1280, 720))
    draw = ImageDraw.Draw(cover, "RGBA")
    draw.rounded_rectangle((30, 510, 1250, 690), 25, fill=(0, 0, 0, 180))
    face = _font(60)
    title = _shape(title[:80])
    while face.getlength(title) > 1150 and face.size > 24:
        face = _font(face.size - 4)
    draw.text((640, 600), title, font=face, fill="white", anchor="mm", stroke_width=2, stroke_fill="black")
    cover.save(destination, "JPEG", quality=85, optimize=True)
    if destination.stat().st_size > 2_000_000:
        raise RuntimeError("Thumbnail exceeds YouTube's 2 MB limit.")


def _choose_thumbnail(images: list[Path]) -> Path:
    def contrast(path):
        with Image.open(path) as image:
            return ImageStat.Stat(image.convert("L").resize((64, 36))).stddev[0]
    return max(images, key=contrast)


async def render_video(
    script: dict, images: list[Path], voices: list[Path], music: Path, job_dir: Path,
    motions: list[Path | None] | None = None,
) -> tuple[Path, Path]:
    scenes = script["scenes"]
    if not scenes or len(images) != len(scenes) or len(voices) != len(scenes):
        raise ValueError("Every scene needs one image and one voice track.")
    ensure_distinct_images(images)
    if motions is not None and len(motions) != len(scenes):
        raise ValueError("Every scene must have exactly one motion entry.")
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    intro = job_dir / "intro.png"
    await asyncio.to_thread(_text_image, script["title"], intro, card=True)
    intro_clip = job_dir / "intro.mp4"
    await asyncio.to_thread(_render_card, intro, intro_clip, 2)
    clips.append(intro_clip)
    total = 4.0
    for index, scene in enumerate(scenes):
        caption = job_dir / f"caption-{index}.png"
        await asyncio.to_thread(_text_image, scene["lyric"], caption)
        duration = max(float(scene["duration"]), await asyncio.to_thread(_duration, voices[index]) + 0.2)
        clip = job_dir / f"scene-{index}.mp4"
        motion = motions[index] if motions else None
        if motion is None:
            motion = job_dir / f"parallax-v2-{index}.mp4"
            await make_parallax_clip(
                images[index], motion, duration, work_dir=job_dir
            )
        if motion is not None and (not Path(motion).is_file() or Path(motion).stat().st_size == 0):
            raise ValueError(f"Motion clip for scene {index + 1} is missing or empty.")
        await asyncio.to_thread(
            _render_scene, images[index], voices[index], caption, clip, duration, motion
        )
        clips.append(clip)
        total += duration
    outro = job_dir / "outro.png"
    await asyncio.to_thread(_text_image, "Subscribe", outro, card=True)
    outro_clip = job_dir / "outro.mp4"
    await asyncio.to_thread(_render_card, outro, outro_clip, 2)
    clips.append(outro_clip)

    playlist = job_dir / "clips.txt"
    playlist.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
    combined = job_dir / "combined.mp4"
    await asyncio.to_thread(_run, [
        "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(playlist),
        "-c", "copy", str(combined),
    ])
    track_duration = await asyncio.to_thread(_duration, music)
    if track_duration < 2:
        raise RuntimeError("Background music must be at least two seconds long.")
    fade = min(1.0, track_duration / 4)
    repeats = max(1, math.ceil((total - fade) / (track_duration - fade)))
    inputs = "".join(f"[m{i}]" for i in range(repeats))
    filters = [f"[1:a]aresample=44100,asplit={repeats}{inputs}"] if repeats > 1 else ["[1:a]aresample=44100[m0]"]
    previous = "m0"
    for index in range(1, repeats):
        filters.append(f"[{previous}][m{index}]acrossfade=d={fade:.3f}[x{index}]")
        previous = f"x{index}"
    filters.extend([
        f"[{previous}]atrim=duration={total:.3f},volume=0.15[bed]",
        "[0:a]asplit=2[voice][side]",
        "[bed][side]sidechaincompress=threshold=0.04:ratio=4[ducked]",
        "[voice][ducked]amix=inputs=2:duration=first:normalize=0[a]",
    ])
    output = job_dir / "final.mp4"
    await asyncio.to_thread(_run, [
        "ffmpeg", "-v", "error", "-y", "-i", str(combined), "-i", str(music),
        "-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(output),
    ])
    thumbnail = job_dir / "thumbnail.jpg"
    chosen = await asyncio.to_thread(_choose_thumbnail, images)
    await asyncio.to_thread(_thumbnail, chosen, script["title"], thumbnail)
    return output, thumbnail
