import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# === SOZLAMALAR ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0")) if os.getenv("ADMIN_ID", "").strip() else 0
MONGO_URI = os.getenv("MONGO_URI", "").strip()
DB_NAME = "kinolar_bot"

# === MONGODB ===
def get_db():
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not configured.")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    return client[DB_NAME]

def ensure_counter():
    db = get_db()
    db["counters"].update_one(
        {"_id": "movie_counter"},
        {"$setOnInsert": {"_id": "movie_counter", "value": 0}},
        upsert=True,
    )

def load_stats():
    db = get_db()
    ensure_counter()
    counter_doc = db["counters"].find_one({"_id": "movie_counter"})
    total = db["movies"].count_documents({})
    codes = [m["code"] for m in db["movies"].find({}, {"_id": 0, "code": 1}).sort("code", -1).limit(10)]
    return total, counter_doc.get("value", 0) if counter_doc else 0, codes

def save_movie(file_id: str, title: str, quality: str, genre: str, duration: str) -> str:
    db = get_db()
    ensure_counter()
    counter_doc = db["counters"].find_one_and_update(
        {"_id": "movie_counter"},
        {"$inc": {"value": 1}},
        return_document=True,
        upsert=True,
    )
    code = str(counter_doc["value"]).zfill(4)
    movie_data = {
        "code": code,
        "file_id": file_id,
        "title": title,
        "quality": quality,
        "genre": genre,
        "duration": duration,
        "views": 0,
        "uploaded_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }
    db["movies"].insert_one(movie_data)
    return code

def get_movie_by_code(code: str):
    db = get_db()
    return db["movies"].find_one({"code": code}, {"_id": 0})


def increment_movie_views(code: str) -> int:
    """Kino ko'rishlar sonini 1 taga oshiradi va yangi qiymatni qaytaradi."""
    db = get_db()
    result = db["movies"].find_one_and_update(
        {"code": code},
        {"$inc": {"views": 1}},
        return_document=True,
    )
    return result.get("views", 0) if result else 0

def delete_movie_by_code(code: str) -> bool:
    db = get_db()
    result = db["movies"].delete_one({"code": code})
    return result.deleted_count > 0


def list_movies(limit: int = 50):
    db = get_db()
    cursor = db["movies"].find(
        {}, {"_id": 0, "code": 1, "title": 1, "genre": 1, "quality": 1, "views": 1}
    ).sort("code", 1).limit(limit)
    return list(cursor)


def update_movie_metadata(code: str, title: str, quality: str, genre: str, duration: str) -> bool:
    db = get_db()
    result = db["movies"].update_one(
        {"code": code},
        {"$set": {
            "title": title,
            "quality": quality,
            "genre": genre,
            "duration": duration,
        }},
    )
    return result.modified_count > 0


def format_movie_caption(movie: dict) -> str:
    return (
        f"🎬 <b>{movie.get('title', 'Nomalum kino')}</b>\n"
        f"📌 Kod: <b>{movie['code']}</b>\n"
        f"🧾 Janri: <b>{movie.get('genre', 'Nomalum')}</b>\n"
        f"🎥 Sifati: <b>{movie.get('quality', 'Nomalum')}</b>\n"
        f"⏱ Davomiyligi: <b>{movie.get('duration', 'Nomalum')}</b>\n"
        f"👁 Ko'rishlar soni: <b>{movie.get('views', 0)}</b>\n"
        f"📅 Botga yuklangan vaqti: <b>{movie.get('uploaded_at', 'Nomalum')}</b>\n\n"
        f"@tomosha_kodi_bot"
    )

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
waiting_for_movie = set()
waiting_for_delete = set()
waiting_for_edit = set()
pending_movie_data = {}
edit_movie_data = {}

# === HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"👋 Salom, <b>{update.effective_user.first_name}</b>!\n\n"
        f"🎬 Kino kodini yozing va to'liq kinoni oling!\n\n"
        f"Masalan: <b>0001</b>",
        parse_mode="HTML",
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Sizda ruxsat yo'q!")
        return

    total, last_code, _ = load_stats()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Kino yuklash", callback_data="upload_movie")],
        [InlineKeyboardButton(text="📃 Kino ro'yxati", callback_data="list_movies")],
        [InlineKeyboardButton(text="✏️ Kino ma'lumotlarini tahrirlash", callback_data="edit_movie")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="stats")],
        [InlineKeyboardButton(text="🗑 Kino o'chirish", callback_data="delete_movie")],
    ])

    await update.message.reply_text(
        f"🎛 <b>Admin Panel</b>\n\n"
        f"📁 Jami kinolar: <b>{total}</b>\n"
        f"🔢 Oxirgi kod: <b>{str(last_code).zfill(4)}</b>\n\n"
        f"Nima qilmoqchisiz?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Sizda ruxsat yo'q!", show_alert=True)
        return

    if query.data == "upload_movie":
        waiting_for_movie.add(query.from_user.id)
        pending_movie_data.pop(query.from_user.id, None)
        await query.message.reply_text("🎬 Kinoni yuboring (video fayl):")
        await query.answer()

    elif query.data == "list_movies":
        movies = list_movies()
        if not movies:
            await query.message.reply_text("📃 Hozircha kino ro'yxati bo'sh.")
        else:
            text = "📃 <b>Kino ro'yxati</b>\n\n"
            text += "\n".join([
                f"<b>{m['code']}</b> — {m.get('title', 'Nomalum')} "
                f"({m.get('quality', 'Nomalum')}, {m.get('genre', 'Nomalum')}) "
                f"— 👁 {m.get('views', 0)}"
                for m in movies
            ])
            await query.message.reply_text(text, parse_mode="HTML")
        await query.answer()

    elif query.data == "edit_movie":
        waiting_for_edit.add(query.from_user.id)
        edit_movie_data.pop(query.from_user.id, None)
        await query.message.reply_text("✏️ Tahrirlash uchun kino kodini yuboring (masalan: 0001):")
        await query.answer()

    elif query.data == "stats":
        total, last_code, codes = load_stats()
        codes_text = "\n".join([f"• {c}" for c in codes]) if codes else "Hali yo'q"
        await query.message.reply_text(
            f"📊 <b>Statistika</b>\n\n"
            f"🎬 Jami kinolar: <b>{total}</b>\n\n"
            f"🔢 Oxirgi 10 ta kod:\n{codes_text}",
            parse_mode="HTML",
        )
        await query.answer()

    elif query.data == "delete_movie":
        waiting_for_delete.add(query.from_user.id)
        await query.message.reply_text("🗑 O'chirmoqchi bo'lgan kino kodini yozing (masalan: 0001):")
        await query.answer()

async def receive_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in waiting_for_movie:
        return
    if update.message is None or update.message.video is None:
        return

    file_id = update.message.video.file_id
    pending_movie_data[user_id] = {
        "file_id": file_id,
        "step": "title",
    }
    waiting_for_movie.discard(user_id)

    await update.message.reply_text(
        "✅ Video olindi.\n"
        "Endi kinoning nomini yuboring:\n"
        "Masalan: <b>Jangovar Sarguzasht</b>",
        parse_mode="HTML",
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.text is None:
        return

    text = message.text.strip()
    user_id = update.effective_user.id

    # O'chirish rejimi
    if user_id in waiting_for_delete and re.fullmatch(r"\d{4}", text):
        waiting_for_delete.discard(user_id)
        code = text.zfill(4)
        if delete_movie_by_code(code):
            await message.reply_text(f"✅ <b>{code}</b> kodi o'chirildi!", parse_mode="HTML")
        else:
            await message.reply_text(f"❌ <b>{code}</b> kodi topilmadi!", parse_mode="HTML")
        return

    # Tahrirlash rejimi: kodni kutish
    if user_id in waiting_for_edit:
        if not re.fullmatch(r"\d{4}", text):
            await message.reply_text(
                "❌ Kod noto'g'ri formatda. Masalan: <b>0001</b>",
                parse_mode="HTML",
            )
            return

        code = text.zfill(4)
        movie = get_movie_by_code(code)
        waiting_for_edit.discard(user_id)

        if not movie:
            await message.reply_text(f"❌ <b>{code}</b> kodi topilmadi!", parse_mode="HTML")
            return

        edit_movie_data[user_id] = {
            "code": code,
            "step": "title",
        }
        await message.reply_text(
            f"✏️ <b>{code}</b> kodli kino tahrirlanmoqda.\n\n"
            f"Joriy nom: <b>{movie.get('title', 'Nomalum')}</b>\n"
            f"Yangi nomni yuboring:",
            parse_mode="HTML",
        )
        return

    # Tahrirlash oqimi: title -> quality -> genre -> duration
    if user_id in edit_movie_data:
        state = edit_movie_data[user_id]
        step = state.get("step")

        if step == "title":
            state["title"] = text
            state["step"] = "quality"
            await message.reply_text(
                "🎥 Yangi sifatni yozing:\n"
                "Masalan: <b>1080p</b>, <b>720p</b>, <b>HD</b>",
                parse_mode="HTML",
            )
            return

        if step == "quality":
            state["quality"] = text
            state["step"] = "genre"
            await message.reply_text(
                "🧾 Yangi janrni yozing:\n"
                "Masalan: <b>Drama</b>, <b>Komediya</b>, <b>Triller</b>",
                parse_mode="HTML",
            )
            return

        if step == "genre":
            state["genre"] = text
            state["step"] = "duration"
            await message.reply_text(
                "⏱ Yangi davomiylikni yozing:\n"
                "Masalan: <b>2 soat 10 daqiqa</b>",
                parse_mode="HTML",
            )
            return

        if step == "duration":
            state["duration"] = text
            code = state["code"]
            title = state.get("title", "Nomalum")
            quality = state.get("quality", "Nomalum")
            genre = state.get("genre", "Nomalum")
            duration = state.get("duration", "Nomalum")

            updated = update_movie_metadata(code, title, quality, genre, duration)
            edit_movie_data.pop(user_id, None)

            if updated:
                await message.reply_text(
                    f"✅ Kino ma'lumotlari yangilandi!\n\n"
                    f"🔢 Kod: <b>{code}</b>\n"
                    f"🎬 Nom: <b>{title}</b>\n"
                    f"📌 Janr: <b>{genre}</b>\n"
                    f"🎥 Sifat: <b>{quality}</b>\n"
                    f"⏱ Davomiyligi: <b>{duration}</b>\n",
                    parse_mode="HTML",
                )
            else:
                await message.reply_text(
                    "⚠️ Ma'lumotlar saqlandi, lekin hech narsa o'zgarmadi "
                    "(bazadan farqi bo'lmagan bo'lishi mumkin)."
                )
            return

    # Admin movie metadata flow (yangi kino qo'shish)
    if user_id in pending_movie_data:
        state = pending_movie_data[user_id]
        step = state.get("step")

        if step == "title":
            state["title"] = text
            state["step"] = "quality"
            await message.reply_text(
                "🎥 Kinoning sifatini yozing:\n"
                "Masalan: <b>1080p</b>, <b>720p</b>, <b>HD</b>",
                parse_mode="HTML",
            )
            return

        if step == "quality":
            state["quality"] = text
            state["step"] = "genre"
            await message.reply_text(
                "🧾 Kinoning janrini yozing:\n"
                "Masalan: <b>Drama</b>, <b>Komediya</b>, <b>Triller</b>",
                parse_mode="HTML",
            )
            return

        if step == "genre":
            state["genre"] = text
            state["step"] = "duration"
            await message.reply_text(
                "⏱ Kinoning davomiyligini yozing:\n"
                "Masalan: <b>2 soat 10 daqiqa</b>",
                parse_mode="HTML",
            )
            return

        if step == "duration":
            state["duration"] = text
            file_id = state["file_id"]
            title = state.get("title", "Nomalum")
            quality = state.get("quality", "Nomalum")
            genre = state.get("genre", "Nomalum")
            duration = state.get("duration", "Nomalum")

            code = save_movie(file_id, title, quality, genre, duration)
            pending_movie_data.pop(user_id, None)

            await message.reply_text(
                f"✅ Kino saqlandi!\n\n"
                f"🔢 Kod: <b>{code}</b>\n"
                f"🎬 Nom: <b>{title}</b>\n"
                f"📌 Janr: <b>{genre}</b>\n"
                f"🎥 Sifat: <b>{quality}</b>\n"
                f"⏱ Davomiyligi: <b>{duration}</b>\n",
                parse_mode="HTML",
            )
            return

    # Kino kodi
    if re.fullmatch(r"\d{4}", text):
        code = text.zfill(4)
        movie = get_movie_by_code(code)
        if movie:
            await message.reply_text("⏳ Kino yuklanmoqda...")
            new_views = increment_movie_views(code)
            movie["views"] = new_views
            await context.bot.send_video(
                chat_id=message.chat_id,
                video=movie["file_id"],
                protect_content=True,
                caption=format_movie_caption(movie),
                parse_mode="HTML",
            )
        else:
            await message.reply_text(
                "❌ Bunday kod topilmadi!\n\n"
                "Kodni to'g'ri yozdingizmi? Masalan: <b>0001</b>",
                parse_mode="HTML",
            )

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured.")
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not configured.")

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("somosab", admin_panel))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.VIDEO, receive_movie))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()