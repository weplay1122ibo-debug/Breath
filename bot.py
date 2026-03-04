import os
import asyncio
from datetime import datetime, timedelta
import asyncpg
import pytz
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Riyadh")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None
user_state = {}

HANDS_LEFT = ["لاشيء", "زوج", "متتالية", "AA"]
HANDS_RIGHT = ["زوجين", "متتالية", "ثلاثة", "فل هاوس", "أربعة"]

# ================= DATABASE =================

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS training (
            id SERIAL PRIMARY KEY,
            side TEXT,
            rank TEXT,
            suit TEXT,
            prev TEXT,
            minute INTEGER,
            result TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)

# ================= AI =================

async def train_ai(side, rank, suit, prev, minute, result):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO training (side, rank, suit, prev, minute, result)
            VALUES ($1,$2,$3,$4,$5,$6)
        """, side, rank, suit, prev, minute, result)

async def predict_ai(side, rank, suit, prev, minute):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT result FROM training WHERE side=$1 AND rank=$2 AND suit=$3 AND prev=$4 AND minute=$5",
            side, rank, suit, prev, minute
        )

    if not rows:
        return "لا يوجد بيانات", 0

    counts = {}
    for r in rows:
        counts[r["result"]] = counts.get(r["result"], 0) + 1

    best = max(counts, key=counts.get)
    confidence = int((counts[best] / len(rows)) * 100)

    return best, confidence

# ================= KEYBOARDS =================

def ranks_kb():
    ranks = ["A","K","Q","J","10","9","8","7","6","5","4","3","2"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=r, callback_data=f"rank_{r}") for r in ranks[i:i+4]]
            for i in range(0, len(ranks), 4)
        ]
    )

def suits_kb():
    suits = ["♥️","♦️","♣️","♠️"]
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=s, callback_data=f"suit_{s}") for s in suits]]
    )

def prev_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=h, callback_data=f"prev_{h}")]
                         for h in HANDS_LEFT]
    )

# ================= BOT FLOW =================

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("اختر رقم الورقة:", reply_markup=ranks_kb())

@dp.callback_query(lambda c: c.data.startswith("rank_"))
async def choose_rank(callback: CallbackQuery):
    user_state[callback.from_user.id] = {"rank": callback.data.split("_")[1]}
    await callback.message.edit_text("اختر النوع:", reply_markup=suits_kb())

@dp.callback_query(lambda c: c.data.startswith("suit_"))
async def choose_suit(callback: CallbackQuery):
    user_state[callback.from_user.id]["suit"] = callback.data.split("_")[1]
    await callback.message.edit_text("اختر الضربة السابقة:", reply_markup=prev_kb())

@dp.callback_query(lambda c: c.data.startswith("prev_"))
async def predict(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    data = user_state.get(user_id)

    if not data:
        await callback.message.edit_text("اكتب /start من جديد")
        return

    tz = pytz.timezone(TIMEZONE)
    minute = datetime.now(tz).minute
    prev = callback.data.replace("prev_", "")

    left, left_conf = await predict_ai("left", data["rank"], data["suit"], prev, minute)
    right, right_conf = await predict_ai("right", data["rank"], data["suit"], prev, minute)

    if user_id == ADMIN_ID:
        await callback.message.edit_text(
            f"يسار: {left} ({left_conf}%)\nيمين: {right} ({right_conf}%)\n\nارسل نتيجة اليسار"
        )
        user_state[user_id]["prev"] = prev
        user_state[user_id]["minute"] = minute
    else:
        await callback.message.edit_text(
            f"⬅️ يسار: {left} ({left_conf}%)\n➡️ يمين: {right} ({right_conf}%)"
        )

# ================= ADMIN TRAIN =================

@dp.message()
async def handle_training(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text in HANDS_LEFT:
        data = user_state.get(message.from_user.id)
        if not data:
            return

        await train_ai("left", data["rank"], data["suit"],
                       data["prev"], data["minute"], message.text)

        await message.answer("ارسل نتيجة اليمين")
        user_state[message.from_user.id]["left_done"] = True

    elif message.text in HANDS_RIGHT:
        data = user_state.get(message.from_user.id)
        if not data:
            return

        await train_ai("right", data["rank"], data["suit"],
                       data["prev"], data["minute"], message.text)

        await message.answer("تم تدريب الذكاء")
        user_state.pop(message.from_user.id, None)

# ================= MAIN =================

async def main():
    await init_db()
    print("Bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())