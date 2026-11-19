"""Original kids-song studio: lyrics -> sung track -> 3D-style scenes -> 16:9 music video.

Self-contained so main.py only wires it in. Provider-specific work (images, motion clips,
fonts) is injected by the caller.
"""
import asyncio
import json
import math
import re
import struct
import subprocess
import wave
from pathlib import Path

import edge_tts
import httpx
from PIL import Image, ImageDraw

SIZE = (1280, 720)
STYLE_3D = (
    "glossy Pixar-style 3D render, adorable chubby characters with big expressive eyes, "
    "soft studio lighting, vibrant saturated colors, smooth toy-like textures, wide 16:9 composition, "
    "bright cheerful background, no text, no logos, original characters"
)
SONG_TOPICS = (
    "counting 1 to 10 with baby animals", "learning colors with a rainbow", "the ABC song with friendly animals",
    "brushing teeth in the morning", "a bedtime lullaby with the moon and stars", "wheels on the little train",
    "dance party with jungle animals", "eating healthy fruits and veggies", "sharing toys with friends",
    "splashing in rain puddles", "the farm animals wake up", "a happy birthday song with a bunny",
    "clean up time", "learning shapes at the playground", "washing hands with bubbles", "a trip to the beach",
)
MUSIC_FALLBACK = "children's nursery rhyme, upbeat, bouncy, kids choir, glockenspiel, ukulele, 105 bpm, joyful, sing-along"
BLENDER_ACTIONS = ("dance", "wave", "clap", "point")


async def write_song(client, model, topic, language, avoid_titles=(), character_instruction=""):
    name = "Arabic" if language == "ar" else "English"
    avoid = f" Do not reuse these earlier titles or ideas: {'; '.join(avoid_titles)}." if avoid_titles else ""
    blender_direction = (
        " For each section also return blender_action, exactly one of dance, wave, clap, point. "
        "The available visuals are limited to: dance in a garden with a ball; wave near a playhouse; "
        "clap in front of a rainbow; point toward colorful blocks. Choose the most suitable action for each section, "
        "and vary the actions. These are the only things the animation can show: do not claim it depicts other actions or objects."
        if character_instruction else ""
    )
    prompt = (
        f"Write an original sing-along children's song for ages 2-6 in {name}. Theme: {topic}.{avoid} "
        f"Follow the structure of top nursery-rhyme channels: one cute recurring main character. {character_instruction} "
        "Use very simple AABB rhymes, "
        "4-7 words per line, a catchy chorus of 4 short lines that repeats 3 times word-for-word, and fun action words kids can copy. "
        "Total song: intro-free, 8 sections in this order: verse, chorus, verse, chorus, verse, chorus, verse, chorus "
        "(each verse and chorus has 4 lines), about 110-170 words in total so it sings in roughly 90-120 seconds. "
        "Never copy any existing song or franchise. Safe, gentle, positive. "
        "Return JSON with: title (catchy, under 70 characters, ending with ' | Kids Songs'), description (2-3 sentences for parents), "
        "tags (10 English and local-language search keywords), art_style (one English sentence), "
        "characters (1-3 objects with name and appearance; appearance is a detailed English description), "
        "music_prompt (English comma-separated music style tags with bpm, instruments and 'kids choir'), "
        "and sections: 8 objects each with type ('verse' or 'chorus'), lines (4 strings in the song language), "
        "visual_description (English, self-contained, names characters, place and action) and motion (one English sentence of character movement). "
        "Chorus sections must use identical lines but different visual_description."
        f"{blender_direction}"
    )
    system = "You are a kids music writer. Write original age-appropriate songs. Return valid JSON only."
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    last = ""
    for _ in range(3):
        try:
            response = await client.chat.completions.create(model=model, messages=messages, response_format={"type": "json_object"})
        except Exception as exc:
            if "response_format" not in str(exc).lower() and "json_object" not in str(exc).lower():
                raise
            response = await client.chat.completions.create(model=model, messages=messages)
        content = response.choices[0].message.content or "{}"
        song = _parse_json(content)
        problem = _validate(song)
        if not problem:
            return song
        last = problem
        messages = messages[:2] + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": f"Problem: {problem}. Return the complete corrected JSON."},
        ]
    raise RuntimeError(f"AI could not write a valid song: {last}")


def _parse_json(text):
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(match.group(0) if match else text)
    except json.JSONDecodeError:
        return {}


def _validate(song):
    sections = song.get("sections")
    if not isinstance(sections, list) or len(sections) < 6:
        return "need at least 6 sections"
    words = 0
    for section in sections:
        lines = section.get("lines") if isinstance(section, dict) else None
        if not isinstance(lines, list) or not 2 <= len(lines) <= 6 or not all(str(x).strip() for x in lines):
            return "every section needs 2-6 non-empty lines"
        if not str(section.get("visual_description", "")).strip():
            return "every section needs a visual_description"
        words += sum(len(str(x).split()) for x in lines)
    if not 90 <= words <= 230:
        return f"song has {words} words; it must have 110-170"
    return ""


def normalize_sections(song, character_sheet):
    scenes = []
    for section in song["sections"][:10]:
        lines = [str(x).strip() for x in section["lines"]]
        scenes.append({
            "type": "chorus" if str(section.get("type", "")).lower() == "chorus" else "verse",
            "lines": lines,
            "narration": " / ".join(lines),
            "visual_description": str(section["visual_description"]).strip(),
            "motion": str(section.get("motion", "")).strip(),
            "character_sheet": character_sheet,
            "blender_action": str(section.get("blender_action", "")).strip().lower(),
        })
    return scenes


def blender_action_indices(scenes):
    return [
        BLENDER_ACTIONS.index(scene["blender_action"])
        if scene.get("blender_action") in BLENDER_ACTIONS
        else index % len(BLENDER_ACTIONS)
        for index, scene in enumerate(scenes)
    ]


def lyrics_text(scenes):
    return "\n\n".join(f"[{s['type']}]\n" + "\n".join(s["lines"]) for s in scenes)


# ---------- audio ----------

def make_backing(path, seconds, bpm=105):
    """Bouncy synthesized backing (kick, hat, pluck melody) used when no AI music provider is set."""
    rate = 22050
    beat = 60.0 / bpm
    scale = [261.63, 293.66, 329.63, 392.0, 440.0, 523.25]
    pattern = [0, 2, 4, 2, 3, 1, 2, 0, 4, 5, 4, 2, 3, 1, 2, 0]
    total = int(seconds * rate)
    buf = [0.0] * total
    step = beat / 2
    for i in range(int(seconds / step) + 1):
        start = int(i * step * rate)
        freq = scale[pattern[i % len(pattern)]]
        for n in range(min(int(rate * 0.28), total - start)):
            t = n / rate
            buf[start + n] += 0.22 * math.exp(-9 * t) * (math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t))
        if i % 2 == 0:
            for n in range(min(int(rate * 0.18), total - start)):
                t = n / rate
                buf[start + n] += 0.5 * math.exp(-18 * t) * math.sin(2 * math.pi * (110 - 220 * t) * t)
        else:
            for n in range(min(int(rate * 0.05), total - start)):
                buf[start + n] += 0.06 * (((n * 7919) % 200) / 100 - 1) * math.exp(-60 * n / rate)
    peak = max(1e-6, max(abs(v) for v in buf))
    frames = b"".join(struct.pack("<h", int(max(-1, min(1, v / peak * 0.8)) * 32767)) for v in buf)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)
    return path


def _run(command, timeout=300):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-500:]}")


async def make_tts_song(scenes, language, folder, voice, audio_duration, bpm=105):
    """Fallback 'sung' track: child-like voice speaking each line on a beat grid over a backing track."""
    bar = 4 * 60.0 / bpm
    pitch = "+22Hz" if language == "en" else "+12Hz"
    parts, timings, cursor = [], [], 0.0
    for si, scene in enumerate(scenes):
        section_start = cursor
        line_times = []
        for li, line in enumerate(scene["lines"]):
            raw = folder / f"line-{si}-{li}.mp3"
            if not raw.exists():
                await asyncio.wait_for(edge_tts.Communicate(line, voice, rate="-8%", pitch=pitch).save(str(raw)), timeout=120)
            slot = max(bar, math.ceil((audio_duration(raw) + 0.15) / bar) * bar)
            wav = folder / f"line-{si}-{li}.wav"
            await asyncio.to_thread(_run, ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-af", f"apad=whole_dur={slot:.3f}", "-t", f"{slot:.3f}", "-ar", "44100", "-ac", "1", str(wav)])
            parts.append(wav)
            line_times.append((cursor - section_start, cursor - section_start + slot))
            cursor += slot
        timings.append((section_start, cursor, line_times))
    playlist = folder / "voice.txt"
    playlist.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    voice_wav = folder / "voice.wav"
    await asyncio.to_thread(_run, ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(playlist), "-c", "copy", str(voice_wav)])
    bed = folder / "backing.wav"
    if not bed.exists():
        await asyncio.to_thread(make_backing, bed, cursor + 1, bpm)
    song = folder / "song.m4a"
    await asyncio.to_thread(_run, ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(voice_wav), "-i", str(bed), "-filter_complex",
                                   "[0:a]volume=1.0[v];[1:a]aresample=44100,volume=0.35[b];[v][b]amix=inputs=2:duration=first:normalize=0[a]", "-map", "[a]", "-c:a", "aac", "-b:a", "128k", str(song)])
    return song, timings


async def make_fal_song(scenes, music_prompt, folder, fal_key, model, audio_duration):
    """Full AI-sung song via fal.ai (default ACE-Step). Section times are spread by lyric length."""
    words = [sum(len(x.split()) for x in s["lines"]) for s in scenes]
    target = max(60, min(150, int(sum(words) * 0.75)))
    headers = {"Authorization": f"Key {fal_key}"}
    body = {"tags": music_prompt or MUSIC_FALLBACK, "lyrics": lyrics_text(scenes), "duration": target}
    try:
        async with httpx.AsyncClient(timeout=120) as http:
            submit = await http.post(f"https://queue.fal.run/{model}", headers=headers, json=body)
            submit.raise_for_status()
            job = submit.json()
            deadline = asyncio.get_running_loop().time() + 900
            while True:
                status = (await http.get(job["status_url"], headers=headers)).json()
                if status.get("status") == "COMPLETED":
                    break
                if status.get("status") not in ("IN_QUEUE", "IN_PROGRESS"):
                    raise RuntimeError(f"fal.ai music generation failed: {str(status)[:300]}")
                if asyncio.get_running_loop().time() > deadline:
                    raise RuntimeError("fal.ai music generation timed out.")
                await asyncio.sleep(6)
            result = (await http.get(job["response_url"], headers=headers)).json()
            audio = await http.get(result["audio"]["url"])
            audio.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"fal.ai music request failed (HTTP {exc.response.status_code}): {exc.response.text[:300]}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Could not reach fal.ai for music.") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("fal.ai music returned an unexpected response.") from exc
    raw = folder / "song-raw.bin"
    raw.write_bytes(audio.content)
    song = folder / "song.m4a"
    await asyncio.to_thread(_run, ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-c:a", "aac", "-b:a", "160k", str(song)])
    total = audio_duration(song)
    weight = sum(words)
    timings, cursor = [], 0.0
    for scene, count in zip(scenes, words):
        span = total * count / weight
        line_times = [(span * i / len(scene["lines"]), span * (i + 1) / len(scene["lines"])) for i in range(len(scene["lines"]))]
        timings.append((cursor, cursor + span, line_times))
        cursor += span
    return song, timings


# ---------- video ----------

def make_lyric_image(text, language, path, font_fn, shape_fn):
    width, height = SIZE[0], 150
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    size = 66
    shaped = shape_fn(text, language)
    face = font_fn(size)
    while face.getlength(shaped) > width - 80 and size > 30:
        size -= 4
        face = font_fn(size)
    draw.rounded_rectangle((30, 10, width - 30, height - 10), radius=36, fill=(20, 10, 60, 150))
    draw.text((width // 2, height // 2), shaped, font=face, fill=(255, 236, 90, 255), anchor="mm", stroke_width=4, stroke_fill=(60, 20, 120, 255))
    image.save(path)


async def render_song_video(scenes, timings, song, language, job_dir, output, make_image, make_motion, font_fn, shape_fn, progress, prebuilt_motion=None, control=None):
    clips = []
    for index, scene in enumerate(scenes):
        if control:
            await control()
        start, end, line_times = timings[index]
        duration = end - start
        key = "chorus" if scene["type"] == "chorus" else index  # repeated chorus reuses one image/clip: saves quota and mirrors real kids songs
        frame = job_dir / f"artwork-{key}.png"
        if prebuilt_motion is None and not frame.exists():
            await make_image(scene, frame)
        motion = job_dir / f"motion-{key}.mp4"
        if prebuilt_motion is not None:
            motion = prebuilt_motion[index % len(prebuilt_motion)]
        elif make_motion and not motion.exists():
            try:
                await make_motion(scene, frame, motion)
            except Exception as exc:
                print(f"Motion failed for scene {index+1}, using zoom: {exc}", flush=True)
        command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y"]
        if motion.exists():
            command += ["-stream_loop", "-1", "-i", str(motion)]
            base = f"[0:v]scale={SIZE[0]}:{SIZE[1]},fps=24,setsar=1,format=yuv420p[base]"
        else:
            command += ["-loop", "1", "-framerate", "24", "-i", str(frame)]
            zoom = "min(1.0+0.0006*on,1.18)" if index % 2 == 0 else "max(1.18-0.0006*on,1.0)"
            base = (f"[0:v]scale=1920:1080,zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)+25*sin(on/24)':y='ih/2-(ih/zoom/2)':"
                    f"d=1:s={SIZE[0]}x{SIZE[1]}:fps=24,setsar=1,format=yuv420p[base]")
        graph, previous = [base], "base"
        for li, line in enumerate(scene["lines"]):
            image = job_dir / f"lyric-{index}-{li}.png"
            make_lyric_image(line, language, image, font_fn, shape_fn)
            command += ["-loop", "1", "-framerate", "24", "-i", str(image)]
            a, b = line_times[li]
            graph.append(f"[{previous}][{li + 1}:v]overlay=x=0:y={SIZE[1] - 190}:enable='between(t,{a:.3f},{b:.3f})':format=auto[l{li}]")
            previous = f"l{li}"
        clip = job_dir / f"clip-{index}.mp4"
        command += ["-filter_complex", ";".join(graph), "-map", f"[{previous}]", "-t", f"{duration:.3f}", "-an",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", str(clip)]
        await asyncio.to_thread(_run, command, 300)
        clips.append(clip)
        progress(int((index + 1) * 85 / len(scenes)))
    playlist = job_dir / "concat.txt"
    playlist.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips), encoding="utf-8")
    silent = job_dir / "silent.mp4"
    await asyncio.to_thread(_run, ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(playlist), "-c", "copy", str(silent)])
    total = timings[-1][1]
    await asyncio.to_thread(_run, ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(silent), "-i", str(song),
                                   "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-af", f"afade=t=out:st={max(0, total - 2):.2f}:d=2",
                                   "-shortest", "-movflags", "+faststart", str(output)])
    return output
