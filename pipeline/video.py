"""Assemble narrated 1080p nursery-rhyme videos with FFmpeg."""

import asyncio
import math
import subprocess
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat


SIZE = (1920, 1080)


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
    if not card:
        draw.rounded_rectangle((100, 790, 1820, 1040), 35, fill=(0, 0, 0, 175))
    face = _font(72 if card else 58)
    lines = text.strip().splitlines()[:2]
    draw.multiline_text(
        (960, 540 if card else 915), "\n".join(_shape(line) for line in lines),
        font=face, fill="white", anchor="mm", align="center", spacing=16,
        stroke_width=2, stroke_fill="black",
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


def _render_card(image: Path, output: Path, seconds: float) -> None:
    _run([
        "ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "24", "-i", str(image),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(seconds),
        "-vf", "format=yuv420p,fade=t=in:st=0:d=0.3,fade=t=out:st=1.7:d=0.3",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-ar", "44100",
        "-ac", "2", str(output),
    ])


def _render_scene(image: Path, voice: Path, captions: Path, output: Path, duration: float) -> None:
    fade_out = max(0, duration - 0.3)
    filters = (
        "[0:v]scale=2304:1296,zoompan=z='min(1+0.0004*on,1.12)':"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=24,"
        f"setsar=1,fade=t=in:st=0:d=0.3,fade=t=out:st={fade_out:.3f}:d=0.3[bg];"
        "[bg][2:v]overlay=0:0:shortest=1,format=yuv420p[v]"
    )
    _run([
        "ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "24", "-i", str(image),
        "-i", str(voice), "-loop", "1", "-framerate", "24", "-i", str(captions),
        "-filter_complex", filters, "-map", "[v]", "-map", "1:a", "-af", "apad",
        "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
        "-ar", "44100", "-ac", "2", str(output),
    ])


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
) -> tuple[Path, Path]:
    scenes = script["scenes"]
    if not scenes or len(images) != len(scenes) or len(voices) != len(scenes):
        raise ValueError("Every scene needs one image and one voice track.")
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
        await asyncio.to_thread(_render_scene, images[index], voices[index], caption, clip, duration)
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
