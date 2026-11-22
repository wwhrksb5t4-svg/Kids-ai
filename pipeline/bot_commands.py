"""Telegram commands for the opt-in automatic nursery-rhyme studio."""

import asyncio
import json
import math
import os
import re
import tempfile
from pathlib import Path

from telegram.ext import CommandHandler

from pipeline import animation, images, script
from pipeline.characters import build_image_prompt, load_characters
from pipeline.guard import check_originality


def register_auto_handlers(application, studio, authorized):
    async def require_admin(update):
        if authorized(update):
            return True
        await update.message.reply_text("غير مصرح.")
        return False

    async def readiness():
        if not studio.channel_id:
            raise ValueError("اضبط CHANNEL_ID أولاً وأضف البوت مديراً للقناة.")
        if not (os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GROQ_API_KEY", "").strip()):
            raise ValueError("اضبط GEMINI_API_KEY أو GROQ_API_KEY أولاً.")
        if not (os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip() and os.getenv("CLOUDFLARE_API_TOKEN", "").strip()):
            raise ValueError("اضبط CLOUDFLARE_ACCOUNT_ID وCLOUDFLARE_API_TOKEN لتوليد الصور.")
        provider = studio.video_provider()
        if provider in animation.FAL_PROVIDERS and not os.getenv("FAL_KEY", "").strip():
            raise ValueError("اضبط FAL_KEY لمزوّد تحريك المشاهد المختار.")
        if provider == "runway" and not os.getenv("RUNWAY_API_KEY", "").strip():
            raise ValueError("اضبط RUNWAY_API_KEY لمزوّد Runway.")
        try:
            await asyncio.to_thread(studio.youtube_services)
        except Exception as exc:
            raise RuntimeError("YouTube غير متصل؛ اربط الحساب باستخدام /connect.") from exc

    async def create(update, context):
        if not await require_admin(update):
            return
        try:
            await readiness()
            job_id = studio.enqueue(" ".join(context.args))
            await update.message.reply_text(f"أُضيفت المهمة {job_id}؛ ستُنشر تلقائياً بعد إنتاجها.")
        except (ValueError, RuntimeError) as exc:
            await update.message.reply_text(str(exc))

    async def addtopic(update, context):
        if not await require_admin(update):
            return
        try:
            job_id = studio.enqueue(" ".join(context.args), "auto", pending=True)
            await update.message.reply_text(f"تم حفظ الموضوع للجدولة: {job_id}")
        except ValueError as exc:
            await update.message.reply_text(str(exc))

    async def video_usage(update, context):
        if not await require_admin(update):
            return
        used, limit, estimated, budget = animation.daily_usage_details(studio.db_path)
        await update.message.reply_text(
            f"استخدام تحريك الفيديو اليوم: {used}/{limit} مشهداً (UTC)، "
            f"التكلفة المقدّرة ${estimated:.2f}/${budget:.2f}. "
            "التكلفة الفعلية قد تختلف؛ هذا ليس سقف فاتورة مضموناً."
        )

    async def auto_on(update, context):
        if not await require_admin(update):
            return
        try:
            if len(context.args) != 1:
                raise ValueError("استخدم /auto_on HOURS بين 1 و168.")
            hours = float(context.args[0])
            if not math.isfinite(hours):
                raise ValueError("أدخل عدداً صالحاً للساعات.")
            await readiness()
            studio.auto_on(hours)
            await update.message.reply_text(
                f"تم تفعيل النشر التلقائي كل {hours:g} ساعة. /auto_off لإيقافه."
            )
        except (ValueError, RuntimeError) as exc:
            await update.message.reply_text(str(exc))

    async def auto_off(update, context):
        if not await require_admin(update):
            return
        studio.auto_off()
        await update.message.reply_text(
            "تم إيقاف الجدولة وإلغاء المهام التلقائية المنتظرة؛ طلب الرفع الجاري قد يكتمل."
        )

    async def setlang(update, context):
        if not await require_admin(update):
            return
        if len(context.args) != 1 or context.args[0] not in ("en", "ar", "both"):
            await update.message.reply_text("استخدم /setlang en أو ar أو both.")
            return
        studio.set_setting("language", context.args[0])
        await update.message.reply_text("تم حفظ اللغة للمهام القادمة.")

    async def setmusic(update, context):
        if not await require_admin(update):
            return
        if len(context.args) != 1 or context.args[0] not in ("library", "generate", "auto"):
            await update.message.reply_text("استخدم /setmusic library أو generate أو auto.")
            return
        studio.set_setting("music", context.args[0])
        await update.message.reply_text("تم حفظ إعداد الموسيقى للمهام القادمة.")

    async def characters(update, context):
        if not await require_admin(update):
            return
        try:
            items = load_characters(studio.character_path)
            await update.message.reply_text(
                "\n".join(f"{item['id']} · {item['name']} · {item['species']}" for item in items)[:3900]
                or "مكتبة الشخصيات فارغة."
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))

    async def preview(update, context):
        if not await require_admin(update):
            return
        if len(context.args) != 1:
            await update.message.reply_text("استخدم /preview CHARACTER_ID.")
            return
        try:
            character_id = context.args[0]
            character = next((item for item in load_characters(studio.character_path) if item["id"] == character_id), None)
            if character is None:
                raise ValueError("الشخصية غير موجودة.")
            await update.message.reply_text("جارٍ توليد المعاينة محلياً؛ قد يستغرق الأمر وقتاً.")
            async with studio.generation_lock:
                with tempfile.TemporaryDirectory(prefix="character-preview-", dir=studio.work_dir) as folder:
                    image = Path(folder) / "preview.png"
                    prompt = build_image_prompt("Front view, full body, standing in a simple studio.", [character_id], path=studio.character_path)
                    await images.generate_image(prompt, character["seed"], image)
                    with image.open("rb") as source:
                        await update.message.reply_photo(photo=source, caption=character["name"])
        except (ValueError, RuntimeError, OSError) as exc:
            await update.message.reply_text(f"تعذر إنشاء المعاينة: {str(exc)[:600]}")

    def save_characters(items):
        from pipeline.characters import load_characters as validate
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".characters-", suffix=".json",
            dir=studio.character_path.parent, delete=False,
        ) as file:
            temporary = Path(file.name)
            try:
                json.dump(items, file, ensure_ascii=False, indent=2)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        try:
            validate(temporary)
            temporary.replace(studio.character_path)
        finally:
            temporary.unlink(missing_ok=True)

    async def newchar(update, context):
        if not await require_admin(update):
            return
        idea = " ".join(context.args).strip()
        if not 3 <= len(idea) <= 500:
            await update.message.reply_text("استخدم /newchar متبوعاً بفكرة بين 3 و500 حرف.")
            return
        try:
            check_originality(idea)
            await update.message.reply_text("جارٍ تصميم الشخصية ومعاينتها محلياً.")
            async with studio.generation_lock:
                existing = load_characters(studio.character_path)
                prompt = (
                    "Create one original child-friendly cartoon character, never imitating any existing property. "
                    "Return only JSON with id (lowercase letters, digits or hyphens), name, "
                    "gender (female or male), species, age_role, "
                    "fixed_visual_description (precise colors, shape, eyes, clothing and one accessory), "
                    "personality, voice (a valid English Edge TTS voice), rate (e.g. -12%), "
                    "pitch (e.g. +5Hz), and seed (an integer from 0 to 4294967295). "
                    f"Idea: {idea}. Existing characters: {json.dumps(existing, ensure_ascii=False)}"
                )
                last_error = None
                for _ in range(3):
                    try:
                        item = script._parse_json(await script.generate_text(prompt))
                        if not isinstance(item, dict) or not re.fullmatch(r"[a-z0-9-]{1,32}", str(item.get("id", ""))):
                            raise ValueError("Invalid character ID.")
                        check_originality(json.dumps(item, ensure_ascii=False))
                        if any(old["id"] == item["id"] or old["seed"] == item.get("seed") for old in existing):
                            raise ValueError("Character ID or seed already exists.")
                        with tempfile.NamedTemporaryFile(
                            mode="w", encoding="utf-8", suffix=".json",
                            dir=studio.character_path.parent, delete=False,
                        ) as candidate:
                            candidate_path = Path(candidate.name)
                            json.dump(existing + [item], candidate, ensure_ascii=False)
                        try:
                            load_characters(candidate_path)
                            with tempfile.TemporaryDirectory(prefix="character-preview-", dir=studio.work_dir) as folder:
                                image = Path(folder) / "preview.png"
                                image_prompt = build_image_prompt(
                                    "Front view, full body, standing in a simple studio.",
                                    [item["id"]], path=candidate_path,
                                )
                                await images.generate_image(image_prompt, item["seed"], image)
                                save_characters(existing + [item])
                                with image.open("rb") as source:
                                    await update.message.reply_photo(photo=source, caption=item["name"])
                            return
                        finally:
                            candidate_path.unlink(missing_ok=True)
                    except (ValueError, KeyError) as exc:
                        last_error = exc
                raise RuntimeError("تعذر إنشاء شخصية صالحة بعد ثلاث محاولات.") from last_error
        except Exception as exc:
            await update.message.reply_text(f"تعذر إنشاء الشخصية: {str(exc)[:600]}")

    async def delchar(update, context):
        if not await require_admin(update):
            return
        if len(context.args) != 1:
            await update.message.reply_text("استخدم /delchar CHARACTER_ID.")
            return
        try:
            async with studio.generation_lock:
                existing = load_characters(studio.character_path)
                remaining = [item for item in existing if item["id"] != context.args[0]]
                if len(remaining) == len(existing):
                    raise ValueError("الشخصية غير موجودة.")
                if not remaining:
                    raise ValueError("يجب إبقاء شخصية واحدة على الأقل.")
                save_characters(remaining)
            await update.message.reply_text("تم حذف الشخصية.")
        except (ValueError, OSError) as exc:
            await update.message.reply_text(str(exc))

    for name, handler in (
        ("create", create), ("addtopic", addtopic), ("auto_on", auto_on),
        ("auto_off", auto_off), ("setlang", setlang), ("setmusic", setmusic),
        ("video_usage", video_usage),
        ("characters", characters), ("preview", preview), ("newchar", newchar), ("delchar", delchar),
    ):
        application.add_handler(CommandHandler(name, handler, block=False))
