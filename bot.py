import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import executor

# ────────────── Configuration ──────────────

API_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

ADMIN_ID = 7717061636
TRAINER_IDS = []  # يمكن إضافة مدربين ديناميكياً

SAUDI_TZ = ZoneInfo("Asia/Riyadh")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

db_pool = None
user_temp = {}  # لتخزين بيانات مؤقتة لكل مستخدم

# ────────────── Game hands ──────────────

LEFT_HANDS = ["none", "sequence_same", "pair", "AA"]
LEFT_HANDS_LABELS = {
    "none": "❌ لا شيء",
    "sequence_same": "♠️ متتالية من نفس النوع",
    "pair": "👥 زوج",
    "AA": "🅰️ AA"
}

RIGHT_HANDS = ["two_pairs", "sequence", "three", "full_house", "four"]
RIGHT_HANDS_LABELS = {
    "two_pairs": "👥 زوجين",
    "sequence": "🔗 متتالية",
    "three": "🎴 ثلاثة",
    "full_house": "🏠 فل هاوس",
    "four": "🂡 أربعة"
}

# ────────────── Database ──────────────

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl="require")

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS training (
                id SERIAL PRIMARY KEY,
                side TEXT NOT NULL,
                rank TEXT,
                suit TEXT,
                prev TEXT,
                result TEXT,
                minute INTEGER,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                expire TIMESTAMP WITH TIME ZONE,
                plan TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS codes (
                code TEXT PRIMARY KEY,
                days INTEGER,
                plan TEXT,
                used BOOLEAN DEFAULT FALSE,
                type TEXT DEFAULT 'user'
            )
        """)

async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID or user_id in TRAINER_IDS:
        return True
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT expire FROM users WHERE user_id = $1", str(user_id))
        return row is not None and row["expire"] > datetime.now(tz=SAUDI_TZ)

async def activate_user(user_id: int, days: int, plan: str, user_type: str = "user"):
    expire = datetime.now(tz=SAUDI_TZ) + timedelta(days=days)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, expire, plan)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET expire = EXCLUDED.expire, plan = EXCLUDED.plan
        """, str(user_id), expire, plan)
    if user_type == "trainer" and user_id not in TRAINER_IDS:
        TRAINER_IDS.append(user_id)

# ────────────── AI / Prediction ──────────────

async def train_ai(side, rank, suit, prev, result):
    minute = datetime.now(tz=SAUDI_TZ).minute
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO training (side, rank, suit, prev, result, minute)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, side, rank, suit, prev, result, minute)

async def predict_hand(side, rank, suit, prev, hands_list):
    scores = {h: 0 for h in hands_list}
    total = 0
    current_minute = datetime.now(tz=SAUDI_TZ).minute

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT rank, suit, prev, result, minute FROM training WHERE side = $1", side)

    for row in rows:
        weight = 0
        if row["rank"] == rank:
            weight += 3
        if row["suit"] == suit:
            weight += 3
        if row["prev"] == prev:
            weight += 5
        if row["minute"] == current_minute and row["result"] in ("AA", "four", "pair"):
            weight += 5
        for res in str(row["result"]).split(","):
            res = res.strip()
            if res in scores:
                scores[res] += weight
                total += weight

    if total == 0:
        best = random.choice(hands_list)
        confidence = random.randint(30, 60)
        return best, confidence

    probabilities = {h: scores[h] / total for h in hands_list}
    rand_val = random.random()
    cumulative = 0.0
    best = None
    for h, p in probabilities.items():
        cumulative += p
        if rand_val <= cumulative:
            best = h
            break
    if random.random() < 0.1:
        best = random.choice(hands_list)

    confidence = int(probabilities.get(best, 0) * 100)
    confidence = max(10, min(100, confidence + random.randint(-5, 5)))
    return best, confidence

# ────────────── Keyboards ──────────────

def ranks_kb():
    ranks = ["A","K","Q","J","10","9","8","7","6","5","4","3","2"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r, callback_data=f"rank_{r}") for r in ranks[i:i+4]]
        for i in range(0, len(ranks), 4)
    ])

def suits_kb():
    suits = ["♥️","♦️","♣️","♠️"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=s, callback_data=f"suit_{s}") for s in suits]
    ])

def prev_hands_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=RIGHT_HANDS_LABELS[h], callback_data=f"prev_{h}") for h in RIGHT_HANDS]
    ])

def next_guess_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 التخمين التالي", callback_data="next_guess")]
    ])

# ────────────── Handlers ──────────────

@dp.message(CommandStart())
async def start(message: Message):
    if not await check_subscription(message.from_user.id):
        await message.answer("❌ لازم تدخل كود اشتراك\n<code>/code XXXXX</code>")
        return
    mode = user_temp.get(message.from_user.id, {}).get("mode", "guess_only")
    text = "🧠 وضع التدريب مفعل" if mode=="training" else "🎲 التخمين العادي"
    await message.answer(text, reply_markup=ranks_kb())

@dp.message(Command("code"))
async def use_code(message: Message):
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("الاستخدام:\n<code>/code XXXXX</code>")
        return
    code = parts[1].upper()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT days, plan, type, used FROM codes WHERE code = $1", code)
        if not row or row["used"]:
            await message.answer("❌ كود غير صالح أو مستخدم")
            return
        await conn.execute("UPDATE codes SET used=TRUE WHERE code=$1", code)
        await activate_user(message.from_user.id, row["days"], row["plan"], row["type"])
        msg = f"🔥 تم التفعيل!\nخطتك: {row['plan']}" if row["type"]=="user" else f"🔥 تم تفعيلك كمدرب!\nخطتك: {row['plan']}"
        await message.answer(msg)

@dp.message(Command("admin"))
async def admin_guess_mode(message: Message):
    parts = message.text.split()
    uid = message.from_user.id
    if uid not in [ADMIN_ID]+TRAINER_IDS:
        return
    if len(parts)==2 and parts[1].lower()=="king":
        user_temp[uid] = {"mode":"guess_only"}
        await message.answer("🎲 وضع التخمين مفعل. اختر رقم الورقة:", reply_markup=ranks_kb())
    else:
        await message.answer("صيغة غير صحيحة")

@dp.message(Command("train"))
async def admin_train_mode(message: Message):
    uid = message.from_user.id
    if uid not in [ADMIN_ID]+TRAINER_IDS:
        return
    user_temp[uid] = {"mode":"training"}
    await message.answer("🧠 وضع التدريب مفعل. اختر رقم الورقة:", reply_markup=ranks_kb())

# ────────────── Callback Handlers ──────────────

@dp.callback_query(lambda c: c.data.startswith("rank_"))
async def choose_rank(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    if uid not in user_temp:
        user_temp[uid] = {}
    user_temp[uid]["rank"] = callback.data.split("_",1)[1]
    await callback.message.edit_text("اختر النوع:", reply_markup=suits_kb())

@dp.callback_query(lambda c: c.data.startswith("suit_"))
async def choose_suit(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    user_temp[uid]["suit"] = callback.data.split("_",1)[1]
    await callback.message.edit_text("اختر الضربة السابقة:", reply_markup=prev_hands_kb())

@dp.callback_query(lambda c: c.data.startswith("prev_"))
async def handle_prev(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    data = user_temp.get(uid)
    if not data or "rank" not in data or "suit" not in data:
        await callback.message.answer("ابدأ من جديد /start")
        return
    prev = callback.data.split("_",1)[1]
    user_temp[uid]["prev"] = prev
    left_pred, left_conf = await predict_hand("left", data["rank"], data["suit"], prev, LEFT_HANDS)
    right_pred, right_conf = await predict_hand("right", data["rank"], data["suit"], prev, RIGHT_HANDS)
    if data.get("mode","guess_only")=="training":
        await train_ai("left", data["rank"], data["suit"], prev, left_pred)
        await train_ai("right", data["rank"], data["suit"], prev, right_pred)
    text = f"⬅️ يسار: {LEFT_HANDS_LABELS.get(left_pred,left_pred)} ({left_conf}%)\n➡️ يمين: {RIGHT_HANDS_LABELS.get(right_pred,right_pred)} ({right_conf}%)"
    await callback.message.edit_text(text, reply_markup=next_guess_kb())

@dp.callback_query(lambda c: c.data=="next_guess")
async def next_guess(callback: CallbackQuery):
    await callback.answer()
    user_temp.pop(callback.from_user.id, None)
    await callback.message.edit_text("ابدأ التخمين الجديد:", reply_markup=ranks_kb())

# ────────────── Main ──────────────

if __name__ == "__main__":
    asyncio.run(init_db())
    logger.info("Bot running via Polling")
    executor.start_polling(dp, skip_updates=True)