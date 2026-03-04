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

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────

API_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ID = 7717061636
TRAINER_IDS = []

SAUDI_TZ = ZoneInfo("Asia/Riyadh")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
db_pool = None
user_temp = {}

# ────────────────────────────────────────────────
# HANDS
# ────────────────────────────────────────────────
LEFT_HANDS = ["none", "sequence_same", "pair", "AA"]
LEFT_LABELS = {
    "none": "❌ لا شيء",
    "sequence_same": "♠️ متتالية نفس النوع",
    "pair": "👥 زوج",
    "AA": "🅰️ AA"
}

RIGHT_HANDS = ["two_pairs", "sequence", "three", "full_house", "four"]
RIGHT_LABELS = {
    "two_pairs": "👥 زوجين",
    "sequence": "🔗 متتالية",
    "three": "🎴 ثلاثة",
    "full_house": "🏠 فل هاوس",
    "four": "🂡 أربعة"
}

# ────────────────────────────────────────────────
# DATABASE
# ────────────────────────────────────────────────
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
                result TEXT,
                minute INTEGER
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                expire TIMESTAMP WITH TIME ZONE,
                role TEXT DEFAULT 'user'
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS codes (
                code TEXT PRIMARY KEY,
                days INTEGER,
                type TEXT DEFAULT 'user',
                active BOOLEAN DEFAULT TRUE
            )
        """)

# ────────────────────────────────────────────────
# SUBSCRIPTIONS
# ────────────────────────────────────────────────
async def check_sub(user_id):
    if user_id == ADMIN_ID or user_id in TRAINER_IDS:
        return True
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT expire, role FROM users WHERE user_id=$1", str(user_id))
        return row and row["expire"] > datetime.now(tz=SAUDI_TZ)

async def activate_user(user_id, days, role="user"):
    expire = datetime.now(tz=SAUDI_TZ) + timedelta(days=days)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, expire, role)
            VALUES ($1, $2, $3)
            ON CONFLICT(user_id) DO UPDATE SET expire=EXCLUDED.expire, role=EXCLUDED.role
        """, str(user_id), expire, role)
    if role=="trainer" and user_id not in TRAINER_IDS:
        TRAINER_IDS.append(user_id)

# ────────────────────────────────────────────────
# TRAINING & PREDICTION
# ────────────────────────────────────────────────
async def train_ai(side, rank, suit, prev, result):
    minute = datetime.now(tz=SAUDI_TZ).minute
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO training (side, rank, suit, prev, result, minute) VALUES ($1,$2,$3,$4,$5,$6)",
                           side, rank, suit, prev, result, minute)

async def predict(side, rank, suit, prev, hands):
    scores = {h:0 for h in hands}
    total = 0
    minute = datetime.now(tz=SAUDI_TZ).minute
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM training WHERE side=$1", side)
    for r in rows:
        weight = 0
        if r["rank"]==rank: weight+=3
        if r["suit"]==suit: weight+=3
        if r["prev"]==prev: weight+=5
        if r["minute"]==minute: weight+=4
        if weight>0 and r["result"] in scores:
            scores[r["result"]]+=weight
            total+=weight
    if total==0: return random.choice(hands), random.randint(30,60)
    best=max(scores, key=scores.get)
    confidence=int((scores[best]/total)*100)
    return best, confidence

# ────────────────────────────────────────────────
# KEYBOARDS
# ────────────────────────────────────────────────
def ranks_kb():
    ranks=["A","K","Q","J","10","9","8","7","6","5","4","3","2"]
    rows=[ranks[i:i+4] for i in range(0,len(ranks),4)]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=r,callback_data=f"rank_{r}") for r in row] for row in rows])

def suits_kb():
    suits=["♥️","♦️","♣️","♠️"]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=s,callback_data=f"suit_{s}") for s in suits]])

def prev_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=RIGHT_LABELS[h],callback_data=f"prev_{h}")] for h in RIGHT_HANDS])

def next_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 تخمين جديد",callback_data="next")]])

# ────────────────────────────────────────────────
# HANDLERS
# ────────────────────────────────────────────────
@dp.message(CommandStart())
async def start(message:Message):
    if not await check_sub(message.from_user.id):
        await message.answer("❌ لازم تفعيل اشتراك /code XXXXX")
        return
    user_temp[message.from_user.id]={}
    await message.answer("اختر رقم الورقة:", reply_markup=ranks_kb())

@dp.callback_query(lambda c:c.data.startswith("rank_"))
async def rank_handler(c:CallbackQuery):
    user_temp[c.from_user.id]["rank"]=c.data.split("_")[1]
    await c.message.edit_text("اختر النوع:", reply_markup=suits_kb())

@dp.callback_query(lambda c:c.data.startswith("suit_"))
async def suit_handler(c:CallbackQuery):
    user_temp[c.from_user.id]["suit"]=c.data.split("_")[1]
    await c.message.edit_text("اختر الضربة السابقة:", reply_markup=prev_kb())

@dp.callback_query(lambda c:c.data.startswith("prev_"))
async def prev_handler(c:CallbackQuery):
    data=user_temp[c.from_user.id]
    prev=c.data.split("_")[1]
    left,l_conf=await predict("left",data["rank"],data["suit"],prev,LEFT_HANDS)
    right,r_conf=await predict("right",data["rank"],data["suit"],prev,RIGHT_HANDS)
    mode=data.get("mode","guess_only")
    if mode=="training":
        await train_ai("left",data["rank"],data["suit"],prev,left)
        await train_ai("right",data["rank"],data["suit"],prev,right)
    await c.message.edit_text(f"⬅️ {LEFT_LABELS[left]} ({l_conf}%)\n➡️ {RIGHT_LABELS[right]} ({r_conf}%)", reply_markup=next_kb())

@dp.callback_query(lambda c:c.data=="next")
async def next_handler(c:CallbackQuery):
    user_temp.pop(c.from_user.id,None)
    await c.message.edit_text("ابدأ من جديد:", reply_markup=ranks_kb())

# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())