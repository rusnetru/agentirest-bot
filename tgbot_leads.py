#!/usr/bin/env python3
"""
tgbot_leads.py — Telegram-бот для сделок Bitrix24 v3.0

Две кнопки:
- 📋 Мои сделки — выбрать и продолжить
- 🆕 Новая заявка — создать сделку

Запуск: python tgbot_leads.py
"""

import asyncio, json, logging, os, re, sys
from datetime import datetime
import requests
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand, BotCommandScopeDefault
)
from aiogram.client.session.aiohttp import AiohttpSession

# ═══════════════ КОНФИГУРАЦИЯ ═══════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_env():
    env = {}
    # Ищем .env в папке скрипта и в home
    paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'),
        r'C:\Users\rusne\Desktop\Битрикс24\.env',
        os.path.expandvars(r'%LOCALAPPDATA%\hermes\.env'),
    ]
    found = False
    for p in paths:
        if os.path.exists(p):
            found = True
            with open(p, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, v = line.split('=', 1)
                        env[k.strip()] = v.strip()
            break
    if not found:
        raise RuntimeError(f".env не найден. Искал: {paths}")
    return env

ENV = _load_env()
TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN не найден")
PROXY_URL = ENV.get("TELEGRAM_PROXY", "")
MANAGER_CHAT_ID = 730367961
B24 = "https://b24-ufslqf.bitrix24.ru/rest/1/wwaict4vpjhiku1s/"
BINDINGS_FILE = os.path.join(SCRIPT_DIR, "tgbot_bindings.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("tgbot")

# ═══════════════ ХРАНИЛИЩЕ ═══════════════
def load_db():
    if os.path.exists(BINDINGS_FILE):
        with open(BINDINGS_FILE, "r", encoding="utf-8") as f: return json.load(f)
    return {}

def save_db(data):
    with open(BINDINGS_FILE, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

def get_client(chat_id):
    db = load_db()
    sid = str(chat_id)
    if sid not in db:
        db[sid] = {"deals": [], "name": "", "username": "", "state": None}
        save_db(db)
    return db[sid]

def update_client(chat_id, c):
    db = load_db()
    db[str(chat_id)] = c
    save_db(db)

def find_client_by_deal(deal_id):
    db = load_db()
    for cid, data in db.items():
        if deal_id in data.get("deals", []): return int(cid), data
    return None, None

# ═══════════════ BITRIX24 ═══════════════
def b24(method, params=None):
    try:
        r = requests.post(B24 + method, json=params or {}, timeout=30)
        data = r.json()
        if "error" in data: logger.error("B24 [%s]: %s", method, data.get("error_description", data["error"]))
        return data
    except Exception as e:
        logger.error("B24 [%s]: %s", method, e)
        return {"error": str(e)}

def create_deal(title, client_name):
    result = b24("crm.deal.add", {
        "fields": {"TITLE": title, "STAGE_ID": "REFINEMENT", "COMMENTS": f"Клиент: {client_name}"}
    })
    if "error" not in result and result.get("result"):
        did = result["result"]
        b24("crm.deal.update", {"ID": did, "fields": {"UF_CRM_LEAD_HASHTAG": f"LD-{did}"}})
        return did
    return None

def get_deal(deal_id):
    r = b24("crm.deal.get", {"ID": deal_id})
    return r.get("result") if "error" not in r else None

def add_timeline(deal_id, text):
    b24("crm.timeline.comment.add", {"fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal", "COMMENT": text}})

def has_business_intent(text):
    patterns = [
        r'(?i)\b(купи[тш]|зака[зж]|приобрест|счёт|счет|оплат|достав[кч]|цена|стоимост|прайс)\b',
        r'(?i)\b(рас[сч]ита[йт]|посчита[йт]|ТЗ|техническое\s*задание|спецификаци[яю])\b',
        r'(?i)\b(подобрать|подбор|аналог|замена|заменит|аналогичны[йе])\b',
        r'(?i)\b(предложени[ея]|КП|смет[ау]|проект)\b',
    ]
    return sum(1 for p in patterns if re.search(p, text)) >= 1

# ═══════════════ КЛАВИАТУРЫ ═══════════════
def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📋 Мои сделки"), KeyboardButton(text="🆕 Новая заявка")]],
        resize_keyboard=True
    )

# ═══════════════ БОТ ═══════════════
if PROXY_URL:
    from aiohttp_socks import ProxyConnector
    session = AiohttpSession(connector=ProxyConnector.from_url(PROXY_URL))
    bot = Bot(token=TOKEN, session=session)
else:
    bot = Bot(token=TOKEN)
dp = Dispatcher()

# ── /start ──
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Здравствуйте! Выберите действие:",
        reply_markup=main_kb()
    )

# ── Кнопки ──
@dp.message(F.text == "📋 Мои сделки")
async def btn_deals(message: Message):
    c = get_client(message.chat.id)
    deals = c.get("deals", [])
    if not deals:
        await message.answer("У вас пока нет сделок.\n\nНажмите «🆕 Новая заявка» чтобы создать.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"LD-{did} — {get_deal(did).get('TITLE','')[:50] if get_deal(did) else f'Сделка #{did}'}", callback_data=f"pick_{did}")]
        for did in deals
    ])
    await message.answer("📋 Ваши сделки. Выберите для продолжения:", reply_markup=kb)

@dp.message(F.text == "🆕 Новая заявка")
async def btn_new(message: Message):
    c = get_client(message.chat.id)
    c["state"] = "awaiting_request"
    update_client(message.chat.id, c)
    await message.answer(
        "Опишите, что нужно:\n"
        "• Подобрать аналоги\n"
        "• Посчитать спецификацию\n"
        "• Заказать оборудование\n\n"
        "Просто напишите ваш запрос.",
        reply_markup=types.ReplyKeyboardRemove()
    )

# ── Выбор сделки (inline) ──
@dp.callback_query(F.data.startswith("pick_"))
async def pick_deal(callback: types.CallbackQuery):
    deal_id = int(callback.data.split("_")[1])
    c = get_client(callback.message.chat.id)
    if deal_id not in c.get("deals", []):
        await callback.answer("Сделка не найдена", show_alert=True)
        return

    c["active_deal"] = deal_id
    c["state"] = None
    update_client(callback.message.chat.id, c)

    deal = get_deal(deal_id)
    title = deal.get("TITLE", "")[:80] if deal else f"Сделка #{deal_id}"
    await callback.message.edit_text(f"✅ Сделка LD-{deal_id}: {title}")
    await callback.message.answer(
        f"Вы в сделке LD-{deal_id}. Пишите ваш вопрос — менеджер ответит.\n\n"
        f"Для смены сделки нажмите кнопку в меню.",
        reply_markup=main_kb()
    )
    await callback.answer()

# ── Текст от клиента ──
@dp.message()
async def handle_text(message: Message):
    if message.chat.id == MANAGER_CHAT_ID:
        await handle_manager(message)
        return

    c = get_client(message.chat.id)
    c["name"] = message.from_user.full_name or "Клиент"
    c["username"] = message.from_user.username or ""
    text = message.text or ""

    # Новая заявка
    if c.get("state") == "awaiting_request":
        if not has_business_intent(text):
            await message.answer("Уточните запрос: что именно нужно — подобрать, посчитать, заказать?")
            return

        title = text[:200].strip()
        await message.answer("⏳ Создаю заявку...")
        deal_id = create_deal(title, c["name"])
        if not deal_id:
            await message.answer("⚠ Ошибка. Попробуйте позже.")
            return

        c["deals"].append(deal_id)
        c["active_deal"] = deal_id
        c["state"] = None
        update_client(message.chat.id, c)

        add_timeline(deal_id, f"💬 {c['name']}: {text}")

        await bot.send_message(
            MANAGER_CHAT_ID,
            f"🆕 Сделка #{deal_id}\n{c['name']} (@{c['username']})\n{title[:200]}\n"
            f"https://b24-ufslqf.bitrix24.ru/crm/deal/details/{deal_id}/"
        )
        await message.answer(
            f"✅ Заявка LD-{deal_id} создана!\nМенеджер скоро ответит.",
            reply_markup=main_kb()
        )
        return

    # Диалог в активной сделке
    active = c.get("active_deal")
    if active:
        add_timeline(active, f"💬 {c['name']}: {text}")
        await bot.send_message(
            MANAGER_CHAT_ID,
            f"📩 {c['name']} (сделка #{active}): {text[:300]}"
        )
        return

    # Нет активной сделки
    await message.answer("Выберите действие:", reply_markup=main_kb())

# ── Менеджер ──
async def handle_manager(message: Message):
    text = message.text or ""
    deal_id = None

    reply = message.reply_to_message
    if reply and reply.text:
        m = re.search(r"сделка #(\d+)|#(\d+)", reply.text, re.I)
        if m: deal_id = int(m.group(1) or m.group(2))

    if deal_id is None:
        m = re.match(r'#(\d+)\s+(.+)', text, re.DOTALL)
        if m:
            deal_id = int(m.group(1))
            text = m.group(2)
        else:
            await message.answer("Ответьте на уведомление или напишите: #ID текст")
            return

    client_chat_id, client_data = find_client_by_deal(deal_id)
    if client_chat_id is None:
        await message.answer(f"⚠ К сделке #{deal_id} не привязан чат.")
        return

    try:
        await bot.send_message(client_chat_id, f"📧 Менеджер (сделка LD-{deal_id}):\n{text}")
        add_timeline(deal_id, f"📤 Менеджер → клиенту: {text}")
        await message.answer(f"✅ Отправлено (сделка #{deal_id})")
    except Exception as e:
        await message.answer(f"⚠ Ошибка: {e}")

# ═══════════════ ЗАПУСК ═══════════════
async def main():
    logger.info("Бот v3.0 запуск")

    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Главная"),
    ], scope=BotCommandScopeDefault())
    await bot.set_my_short_description("Заявки и сделки")

    try:
        await bot.send_message(MANAGER_CHAT_ID, "🤖 Бот запущен.")
    except: pass

    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Стоп")
