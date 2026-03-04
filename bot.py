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
from aiohttp import web

# ─── Configuration ───
API_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
PORT = int(os.getenv("PORT", 8080))

ADMIN_ID = 7717061636
TRAINER_IDS = []  # يتم إضافتهم ديناميكيًا
SAUDI_TZ = ZoneInfo("Asia/Riyadh")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

db_pool = None
user_temp = {}  # مؤقت للمستخدمين

# ─── Hands ───
LEFT_HANDS = ["none", "sequence_same", "pair", "AA"]
LEFT_LABELS = {
    "none": "❌ لا شيء",
    "sequence_same": "♠️ متتالية من نفس النوع",
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

# ─── Database ───
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, ssl="require")

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
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                expire TIMESTAMPTZ,
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
        row = await conn.fetchrow("SELECT expire FROM users WHERE user_id=$1", str(user_id))
        return row is not None and row["expire"] > datetime.now(tz=SAUDI_TZ)

async def activate_user(user_id: int, days: int, plan: str, user_type: str = "user"):
    expire = datetime.now(tz=SAUDI_TZ) + timedelta(days=days)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users(user_id, expire, plan)
            VALUES($1,$2,$3)
            ON CONFLICT(user_id) DO UPDATE
            SET expire = EXCLUDED.expire, plan=EXCLUDED.plan
        """, str(user_id), expire, plan)
    if user_type == "trainer" and user_id not in TRAINER_IDS:
        TRAINER_IDS.append(user_id)

# ─── AI / Prediction ───
async def train_ai(side: str, rank: str, suit: str, prev: str, result: str):
    minute = datetime.now(tz=SAUDI_TZ).minute
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO training(side, rank, suit, prev, result, minute)
            VALUES($1,$2,$3,$4,$5,$6)
        """, side, rank, suit, prev, result, minute)

async def predict_hand(side: str, rank: str, suit: str, prev: str, hands: list[str]):
    scores = {h:0 for h in hands}
    total = 0
    current_minute = datetime.now(tz=SAUDI_TZ).minute
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT rank, suit, prev, result, minute FROM training WHERE side=$1", side)
    for row in rows:
        weight = 0
        if row["rank"]==rank: weight+=3
        if row["suit"]==suit: weight+=3
        if row["prev"]==prev: weight+=5
        if row["minute"]==current_minute and row["result"] in ("AA","four","pair"): weight+=5
        for res in str(row["result"]).split(","):
            if res in scores: scores[res]+=weight; total+=weight
    if total==0: return random.choice(hands), random.randint(30,60)
    probs={h:scores[h]/total for h in hands}
    rand=random.random(); cum=0.0; best=None
    for h,p in probs.items():
        cum+=p
        if rand<=cum: best=h; break
    if random.random()<0.1: best=random.choice(hands)
    confidence=int(probs.get(best,0)*100)
    return best, max(10,min(100,confidence+random.randint(-5,5)))

# ─── Keyboards ───
def ranks_kb():
    ranks = ["A","K","Q","J","10","9","8","7","6","5","4","3","2"]
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(r,f"rank_{r}") for r in ranks[i:i+4]] for i in range(0,len(ranks),4)]
    )
def suits_kb():
    suits = ["♥️","♦️","♣️","♠️"]
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(s,f"suit_{s}") for s in suits]]
    )
def prev_hands_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(RIGHT_LABELS[h],f"prev_{h}")] for h in RIGHT_HANDS]
    )
def next_guess_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton("🔄 التخمين التالي","next_guess")]]
    )

# ─── Handlers ───
@dp.message(CommandStart())
async def start(message:Message):
    if not await check_subscription(message.from_user.id):
        await message.answer("❌ لازم تدخل كود اشتراك\n<code>/code XXXXX</code>"); return
    mode=user_temp.get(message.from_user.id,{}).get("mode","guess_only")
    await message.answer("🧠 وضع التدريب مفعل" if mode=="training" else "🎲 التخمين العادي", reply_markup=ranks_kb())

@dp.message(Command("code"))
async def use_code(message:Message):
    parts=message.text.split()
    if len(parts)!=2: await message.answer("<code>/code XXXXX</code>"); return
    code=parts[1].upper()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT days, plan, type, used FROM codes WHERE code=$1", code)
        if not row or row["used"]: await message.answer("❌ كود غير صالح"); return
        await conn.execute("UPDATE codes SET used=TRUE WHERE code=$1", code)
        await activate_user(message.from_user.id,row["days"],row["plan"],row["type"])
        await message.answer(f"🔥 تم التفعيل! نوعك: {row['type']}, خطتك: {row['plan']}")

@dp.message(Command("admin"))
async def admin_guess_mode(message:Message):
    uid=message.from_user.id
    if uid not in [ADMIN_ID]+TRAINER_IDS: return
    if len(message.text.split())==2 and message.text.split()[1].lower()=="king":
        user_temp[uid]={"mode":"guess_only"}
        await message.answer("🎲 وضع التخمين مفعل", reply_markup=ranks_kb())

@dp.message(Command("train"))
async def admin_train_mode(message:Message):
    uid=message.from_user.id
    if uid not in [ADMIN_ID]+TRAINER_IDS: return
    user_temp[uid]={"mode":"training"}
    await message.answer("🧠 وضع التدريب مفعل", reply_markup=ranks_kb())

@dp.callback_query(lambda c:c.data.startswith("rank_"))
async def choose_rank(callback:CallbackQuery):
    uid=callback.from_user.id; user_temp.setdefault(uid,{})["rank"]=callback.data.split("_",1)[1]
    await callback.message.edit_text("اختر النوع:", reply_markup=suits_kb())
    await callback.answer()

@dp.callback_query(lambda c:c.data.startswith("suit_"))
async def choose_suit(callback:CallbackQuery):
    uid=callback.from_user.id; user_temp.setdefault(uid,{})["suit"]=callback.data.split("_",1)[1]
    await callback.message.edit_text("اختر الضربة السابقة:", reply_markup=prev_hands_kb())
    await callback.answer()

@dp.callback_query(lambda c:c.data.startswith("prev_"))
async def handle_prev(callback:CallbackQuery):
    uid=callback.from_user.id
    data=user_temp.get(uid); await callback.answer()
    if not data or "rank" not in data or "suit" not in data: await callback.message.answer("ابدأ /start"); return
    prev=data["prev"]=callback.data.split("_",1)[1]
    left_pred,left_conf=await predict_hand("left", data["rank"], data["suit"], prev, LEFT_HANDS)
    right_pred,right_conf=await predict_hand("right", data["rank"], data["suit"], prev, RIGHT_HANDS)
    if data.get("mode","guess_only")=="training":
        await train_ai("left",data["rank"],data["suit"],prev,left_pred)
        await train_ai("right",data["rank"],data["suit"],prev,right_pred)
    await callback.message.edit_text(f"⬅️ يسار: {LEFT_LABELS.get(left_pred,left_pred)} ({left_conf}%)\n➡️ يمين: {RIGHT_LABELS.get(right_pred,right_pred)} ({right_conf}%)", reply_markup=next_guess_kb())

@dp.callback_query(lambda c:c.data=="next_guess")
async def next_guess(callback:CallbackQuery):
    user_temp.pop(callback.from_user.id,None)
    await callback.message.edit_text("ابدأ التخمين الجديد:", reply_markup=ranks_kb())
    await callback.answer()

# ─── Main ───
async def main():
    await init_db()
    from aiogram import F
    await bot.delete_webhook(drop_pending_updates=True)
    runner=web.AppRunner(web.Application())
    await runner.setup()
    site=web.TCPSite(runner,"0.0.0.0",PORT)
    await site.start()
    logger.info("Bot running on port %s",PORT)
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())