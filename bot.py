import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandStart

# ================= CONFIG =================
API_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

ADMIN_ID = 7717061636
TRAINER_IDS = []

SAUDI_TZ = ZoneInfo("Asia/Riyadh")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
db_pool = None
user_temp = {}

# ================= HANDS =================
LEFT_HANDS = ["none", "sequence_same", "pair", "AA"]
LEFT_LABELS = {"none": "❌ لا شيء", "sequence_same": "♠️ متتالية من نفس النوع", "pair": "👥 زوج", "AA": "🅰️ AA"}
RIGHT_HANDS = ["two_pairs", "sequence", "three", "full_house", "four"]
RIGHT_LABELS = {"two_pairs": "👥 زوجين", "sequence": "🔗 متتالية", "three": "🎴 ثلاثة", "full_house": "🏠 فل هاوس", "four": "🂡 أربعة"}

# ================= DATABASE =================
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, ssl="require")
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS training (
                id SERIAL PRIMARY KEY,
                side TEXT,
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
        row = await conn.fetchrow("SELECT expire FROM users WHERE user_id=$1", str(user_id))
    return row is not None and row["expire"] > datetime.now(SAUDI_TZ)

async def activate_user(user_id: int, days: int, plan: str, user_type: str="user"):
    expire = datetime.now(SAUDI_TZ) + timedelta(days=days)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, expire, plan)
            VALUES ($1,$2,$3)
            ON CONFLICT(user_id) DO UPDATE
            SET expire=EXCLUDED.expire, plan=EXCLUDED.plan
        """, str(user_id), expire, plan)
    if user_type=="trainer" and user_id not in TRAINER_IDS:
        TRAINER_IDS.append(user_id)

# ================= AI =================
async def train_ai(side, rank, suit, prev, result):
    minute = datetime.now(SAUDI_TZ).minute
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO training (side, rank, suit, prev, result, minute)
            VALUES ($1,$2,$3,$4,$5,$6)
        """, side, rank, suit, prev, result, minute)

async def predict_hand(side, rank, suit, prev, hands_list):
    scores = {h:0 for h in hands_list}
    total = 0
    current_minute = datetime.now(SAUDI_TZ).minute
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT rank,suit,prev,result,minute FROM training WHERE side=$1", side)
    for r in rows:
        weight = 0
        if r["rank"]==rank: weight+=3
        if r["suit"]==suit: weight+=3
        if r["prev"]==prev: weight+=5
        if r["minute"]==current_minute and r["result"] in ("AA","four","pair"): weight+=5
        if weight>0:
            for res in str(r["result"]).split(","):
                res=res.strip()
                if res in scores:
                    scores[res]+=weight
                    total+=weight
    if total==0:
        best=random.choice(hands_list)
        confidence=random.randint(30,60)
        return best, confidence
    probabilities={h:scores[h]/total for h in hands_list}
    rand_val=random.random()
    cumulative=0
    best=None
    for h,p in probabilities.items():
        cumulative+=p
        if rand_val<=cumulative:
            best=h
            break
    if random.random()<0.1:
        best=random.choice(hands_list)
    confidence=int(probabilities.get(best,0)*100)
    confidence=max(10,min(100,confidence+random.randint(-5,5)))
    return best, confidence

# ================= KEYBOARDS =================
def ranks_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=r, callback_data=f"rank_{r}") for r in row] for row in [ ["A","K","Q","J"],["10","9","8","7"],["6","5","4","3"],["2"] ]])
def suits_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=s, callback_data=f"suit_{s}") for s in ["♥️","♦️","♣️","♠️"]]])
def prev_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=RIGHT_LABELS[h], callback_data=f"prev_{h}")] for h in RIGHT_HANDS])
def next_guess_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 التخمين التالي", callback_data="next_guess")]])

# ================= HANDLERS =================
@dp.message(CommandStart())
async def start(message:Message):
    if not await check_subscription(message.from_user.id):
        await message.answer("❌ لازم تدخل كود اشتراك\n/code XXXXX")
        return
    await message.answer("🎲 اختر رقم الورقة:", reply_markup=ranks_kb())

@dp.message(Command("code"))
async def use_code(message:Message):
    parts=message.text.split()
    if len(parts)!=2:
        await message.answer("الاستخدام:\n/code XXXXX")
        return
    code=parts[1].upper()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT days,plan,type,used FROM codes WHERE code=$1", code)
        if not row or row["used"]:
            await message.answer("❌ كود غير صالح أو مستخدم")
            return
        await conn.execute("UPDATE codes SET used=TRUE WHERE code=$1", code)
        await activate_user(message.from_user.id, row["days"], row["plan"], row["type"])
        await message.answer(f"✅ تم التفعيل! خطتك: {row['plan']}")

@dp.message(Command("admin"))
async def admin_guess(message:Message):
    if message.from_user.id not in [ADMIN_ID]+TRAINER_IDS: return
    parts=message.text.split()
    if len(parts)==2 and parts[1].lower()=="king":
        user_temp[message.from_user.id]={"mode":"guess_only"}
        await message.answer("🎲 وضع التخمين مفعل. اختر رقم الورقة:", reply_markup=ranks_kb())

@dp.message(Command("train"))
async def admin_train(message:Message):
    if message.from_user.id not in [ADMIN_ID]+TRAINER_IDS: return
    user_temp[message.from_user.id]={"mode":"training"}
    await message.answer("🧠 وضع التدريب مفعل. اختر رقم الورقة:", reply_markup=ranks_kb())

@dp.callback_query(lambda c: c.data.startswith("rank_"))
async def choose_rank(cb:CallbackQuery):
    uid=cb.from_user.id
    if uid not in user_temp: user_temp[uid]={}
    user_temp[uid]["rank"]=cb.data.split("_",1)[1]
    await cb.message.edit_text("اختر النوع:", reply_markup=suits_kb())

@dp.callback_query(lambda c: c.data.startswith("suit_"))
async def choose_suit(cb:CallbackQuery):
    uid=cb.from_user.id
    user_temp[uid]["suit"]=cb.data.split("_",1)[1]
    await cb.message.edit_text("اختر الضربة السابقة:", reply_markup=prev_kb())

@dp.callback_query(lambda c: c.data.startswith("prev_"))
async def handle_prev(cb:CallbackQuery):
    uid=cb.from_user.id
    data=user_temp.get(uid)
    if not data or "rank" not in data or "suit" not in data:
        await cb.message.answer("ابدأ من جديد /start")
        return
    prev=cb.data.split("_",1)[1]
    user_temp[uid]["prev"]=prev
    left_pred,left_conf=await predict_hand("left",data["rank"],data["suit"],prev,LEFT_HANDS)
    right_pred,right_conf=await predict_hand("right",data["rank"],data["suit"],prev,RIGHT_HANDS)
    mode=data.get("mode","guess_only")
    if mode=="training":
        await train_ai("left",data["rank"],data["suit"],prev,left_pred)
        await train_ai("right",data["rank"],data["suit"],prev,right_pred)
    text=f"⬅️ يسار: {LEFT_LABELS.get(left_pred,left_pred)} ({left_conf}%)\n➡️ يمين: {RIGHT_LABELS.get(right_pred,right_pred)} ({right_conf}%)"
    await cb.message.edit_text(text, reply_markup=next_guess_kb())

@dp.callback_query(lambda c: c.data=="next_guess")
async def next_guess(cb:CallbackQuery):
    uid=cb.from_user.id
    user_temp.pop(uid,None)
    await cb.message.edit_text("ابدأ التخمين الجديد:", reply_markup=ranks_kb())

# ================= MAIN =================
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())