#!/usr/bin/env python3
"""
tgbot_leads.py — Telegram-бот для работы со сделками Bitrix24 v2.0

Сценарий:
- Клиент пишет в бот запрос → создаётся СДЕЛКА (если есть суть: купить/посчитать/подобрать)
- AI/менеджер общается с клиентом через чат сделки
- Каждое сообщение содержит хештег LD-{ID}
- Клиент переключается между сделками: #ID
- /deals — список своих сделок
- Менеджер отвечает через реплай или #ID текст

Запуск: python tgbot_leads.py
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

# ═══════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Telegram — токен из .env (безопасность: не хранить в репозитории!)
def _load_token():
    paths = [
        os.path.expandvars(r'%LOCALAPPDATA%\hermes\.env'),
        os.path.join(SCRIPT_DIR, '.env'),
    ]
    for p in paths:
        try:
            with open(p, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if 'TELEGRAM_BOT_TOKEN' in line and not line.startswith('#'):
                        return line.split('=', 1)[1].strip()
        except FileNotFoundError:
            continue
    raise RuntimeError("TELEGRAM_BOT_TOKEN не найден. Создайте .env файл.")

TOKEN = _load_token()

MANAGER_CHAT_ID = 730367961
MANAGER_NAME = "Иван Иванов"

# Bitrix24
B24 = "https://b24-ufslqf.bitrix24.ru/rest/1/wwaict4vpjhiku1s/"

# Хранилище
BINDINGS_FILE = os.path.join(SCRIPT_DIR, "tgbot_bindings.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("tgbot")

# ═══════════════════════════════════════════
# ХРАНИЛИЩЕ
# ═══════════════════════════════════════════

def load_db():
    if os.path.exists(BINDINGS_FILE):
        with open(BINDINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_db(data):
    with open(BINDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_client(chat_id):
    db = load_db()
    sid = str(chat_id)
    if sid not in db:
        db[sid] = {"deals": [], "active_deal": None, "name": "", "username": ""}
        save_db(db)
    return db[sid]

def update_client(chat_id, client):
    db = load_db()
    db[str(chat_id)] = client
    save_db(db)

def find_client_by_deal(deal_id):
    """Найти chat_id клиента по ID сделки."""
    db = load_db()
    for cid, data in db.items():
        if deal_id in data.get("deals", []):
            return int(cid), data
    return None, None

# ═══════════════════════════════════════════
# BITRIX24 API
# ═══════════════════════════════════════════

def b24(method, params=None):
    try:
        r = requests.post(B24 + method, json=params or {}, timeout=30)
        data = r.json()
        if "error" in data:
            logger.error("B24 [%s]: %s", method, data.get("error_description", data["error"]))
        return data
    except Exception as e:
        logger.error("B24 [%s]: %s", method, e)
        return {"error": str(e)}

def create_deal(title, client_name, contact_info=""):
    """Создать сделку с начальной стадией CLASSIFICATION."""
    result = b24("crm.deal.add", {
        "fields": {
            "TITLE": title,
            "STAGE_ID": "CLASSIFICATION",
            "OPPORTUNITY": 0,
            "COMMENTS": f"Клиент: {client_name}\n{contact_info}",
        }
    })
    if "error" not in result and result.get("result"):
        deal_id = result["result"]
        # Сразу присваиваем хештег
        b24("crm.deal.update", {
            "ID": deal_id,
            "fields": {"UF_CRM_LEAD_HASHTAG": f"LD-{deal_id}"},
        })
        return deal_id
    return None

def get_deal(deal_id):
    result = b24("crm.deal.get", {"ID": deal_id})
    if "error" not in result:
        return result.get("result")
    return None

def get_client_deals_from_b24(client_name):
    """Найти все сделки клиента из Bitrix24 по имени."""
    # Ищем по заголовкам, содержащим имя клиента
    result = b24("crm.deal.list", {
        "filter": {"%TITLE": client_name},
        "select": ["ID", "TITLE", "STAGE_ID", "DATE_CREATE"],
    })
    if "error" not in result:
        return result.get("result", [])
    return []

def add_to_timeline(deal_id, text):
    b24("crm.timeline.comment.add", {
        "fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal", "COMMENT": text}
    })

def update_stage(deal_id, stage_id):
    b24("crm.deal.update", {"ID": deal_id, "fields": {"STAGE_ID": stage_id}})

# ═══════════════════════════════════════════
# КЛАССИФИКАЦИЯ ЗАПРОСА
# ═══════════════════════════════════════════

def has_business_intent(text):
    """Проверяет, содержит ли сообщение деловую суть (закупка/расчёт)."""
    patterns = [
        r'(?i)\b(купи[тш]|зака[зж]|приобрест|счёт|счет|оплат|достав[кч]|цена|стоимост|прайс)\b',
        r'(?i)\b(рас[сч]чита[йт]|посчита[йт]|ТЗ|техническое\s*задание|спецификаци[яю])\b',
        r'(?i)\b(подобрать|подбор|аналог|замена|заменит|аналогичны[йе])\b',
        r'(?i)\b(предложени[ея]|коммерческ|КП|смет[ау]|проект)\b',
    ]
    score = sum(1 for p in patterns if re.search(p, text))
    return score >= 1

def extract_deal_title(text):
    """Извлечь заголовок сделки из сообщения."""
    # Берём первые 200 символов, обрезаем по точке или переносу строки
    title = text[:200].strip()
    for sep in ['. ', '.\n', '\n\n', '\n']:
        if sep in title:
            title = title.split(sep)[0].strip()
            break
    # Убираем мусор
    title = re.sub(r'\s+', ' ', title)
    if len(title) < 10:
        title = f"Запрос: {title}"
    return title

# ═══════════════════════════════════════════
# БОТ
# ═══════════════════════════════════════════

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ── Команды ──

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Добро пожаловать!\n\n"
        "Я помогу вам с подбором оборудования и расчётом спецификаций.\n"
        "Просто опишите, что вам нужно — и я создам заявку.\n\n"
        "Команды:\n"
        "/deals — мои сделки\n"
        "/help — помощь"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📋 *Как работать с ботом:*\n\n"
        "1️⃣ Напишите, что хотите купить или посчитать\n"
        "2️⃣ Бот создаст сделку и присвоит номер (LD-123)\n"
        "3️⃣ Менеджер ответит вам здесь же\n"
        "4️⃣ Чтобы переключиться на другую сделку — напишите её номер: `#123`\n"
        "5️⃣ `/deals` — посмотреть все свои сделки\n\n"
        "Все ваши сделки видны только вам.",
        parse_mode="Markdown"
    )

@dp.message(Command("deals"))
async def cmd_deals(message: Message):
    chat_id = message.chat.id
    client = get_client(chat_id)
    deals = client.get("deals", [])

    if not deals:
        await message.answer("У вас пока нет сделок. Напишите, что вас интересует!")
        return

    lines = ["📋 *Ваши сделки:*\n"]
    for did in deals:
        deal = get_deal(did)
        if deal:
            title = deal.get("TITLE", f"Сделка #{did}")[:60]
            stage = deal.get("STAGE_ID", "?")
            marker = " ◀ *активна*" if did == client.get("active_deal") else ""
            lines.append(f"`LD-{did}` {title}{marker}")
        else:
            lines.append(f"`LD-{did}` (не найдена в CRM)")

    lines.append(f"\n_Активная сделка: {'LD-' + str(client['active_deal']) if client.get('active_deal') else 'нет'}_")
    lines.append("\nДля переключения напишите `#номер` (например `#4`)")
    await message.answer("\n".join(lines), parse_mode="Markdown")

# ── Обработка сообщений ──

@dp.message()
async def handle_message(message: Message):
    chat_id = message.chat.id
    text = message.text or message.caption or ""
    client_name = message.from_user.full_name or "Клиент"
    username = message.from_user.username or ""

    logger.info("[%s] %s: %s", chat_id, client_name, text[:100])

    # ── Менеджер ──
    if chat_id == MANAGER_CHAT_ID:
        await handle_manager(message)
        return

    # ── Клиент ──
    client = get_client(chat_id)
    client["name"] = client_name
    client["username"] = username

    # Проверка: переключение сделки? #N
    switch = re.match(r'#(\d+)\s*(.*)', text, re.DOTALL)
    if switch:
        deal_id = int(switch.group(1))
        rest = switch.group(2).strip()

        if deal_id not in client.get("deals", []):
            await message.answer(f"⚠ Сделка LD-{deal_id} не найдена среди ваших. /deals — посмотреть список.")
            return

        client["active_deal"] = deal_id
        update_client(chat_id, client)

        deal = get_deal(deal_id)
        title = deal.get("TITLE", "")[:100] if deal else f"Сделка #{deal_id}"
        await message.answer(f"✅ Переключились на сделку `LD-{deal_id}`: {title}", parse_mode="Markdown")

        if rest:
            # Есть текст после #N — отправляем его в сделку
            await process_client_message(message, client, rest, deal_id)
        return

    # Активная сделка? Продолжаем диалог
    active = client.get("active_deal")
    if active:
        await process_client_message(message, client, text, active)
        return

    # Новый запрос — проверяем на бизнес-суть
    if has_business_intent(text):
        await create_new_deal(message, client, text)
    else:
        await message.answer(
            "Уточните, пожалуйста: что именно вас интересует?\n\n"
            "Например:\n"
            "• «Подобрать аналоги насосов Grundfos»\n"
            "• «Посчитать спецификацию по ТЗ»\n"
            "• «Нужна замена датчиков давления»\n\n"
            "Я сразу создам заявку, и менеджер подключится."
        )

# ── Создание сделки ──

async def create_new_deal(message, client, text):
    chat_id = message.chat.id
    client_name = client["name"]

    title = extract_deal_title(text)
    await message.answer(f"⏳ Создаю заявку: *{title[:100]}*...", parse_mode="Markdown")

    deal_id = create_deal(title, client_name, f"Telegram @{client.get('username', '')} (chat_id={chat_id})")

    if not deal_id:
        await message.answer("⚠ Не удалось создать заявку. Попробуйте позже.")
        return

    # Обновляем клиента
    client["deals"].append(deal_id)
    client["active_deal"] = deal_id
    update_client(chat_id, client)

    # В таймлайн
    add_to_timeline(deal_id, f"💬 Клиент: {text}")
    # Следующая стадия — проработка
    update_stage(deal_id, "REFINEMENT")

    # Клавиатура для менеджера
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Ответить клиенту", callback_data=f"reply_{deal_id}")
    ]])

    await bot.send_message(
        MANAGER_CHAT_ID,
        f"🆕 *Новая сделка #{deal_id}*\n"
        f"Клиент: {client_name} (@{client.get('username', '')})\n"
        f"Запрос: {title[:200]}\n"
        f"[Открыть в CRM](https://b24-ufslqf.bitrix24.ru/crm/deal/details/{deal_id}/)",
        parse_mode="Markdown",
        reply_markup=kb,
    )

    await message.answer(
        f"✅ Заявка *LD-{deal_id}* создана!\n"
        f"Менеджер скоро подключится. Все сообщения в этом чате пойдут в эту сделку.\n\n"
        f"Чтобы переключиться на другую сделку — напишите её номер: `#номер`\n"
        f"/deals — посмотреть все сделки",
        parse_mode="Markdown"
    )

# ── Сообщение клиента в активную сделку ──

async def process_client_message(message, client, text, deal_id):
    chat_id = message.chat.id
    client_name = client["name"]

    # Запись в таймлайн сделки
    add_to_timeline(deal_id, f"💬 {client_name}: {text}")

    # Уведомление менеджеру
    deal = get_deal(deal_id)
    title = deal.get("TITLE", "")[:80] if deal else f"Сделка #{deal_id}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply_{deal_id}")
    ]])

    await bot.send_message(
        MANAGER_CHAT_ID,
        f"📩 *{client_name}* (сделка #{deal_id}): {text[:300]}",
        parse_mode="Markdown",
        reply_markup=kb,
    )

# ── Менеджер ──

async def handle_manager(message):
    text = message.text or message.caption or ""

    # Попытка извлечь deal_id из реплая
    deal_id = None
    reply = message.reply_to_message
    if reply and reply.text:
        m = re.search(r"сделка #(\d+)|#(\d+)", reply.text, re.I)
        if m:
            deal_id = int(m.group(1) or m.group(2))

    # Или из текста: #N текст
    if deal_id is None:
        m = re.match(r'#(\d+)\s+(.+)', text, re.DOTALL)
        if m:
            deal_id = int(m.group(1))
            text = m.group(2)
        else:
            await message.answer(
                "Чтобы ответить клиенту:\n"
                "• Ответьте (реплай) на сообщение о сделке\n"
                "• Или: `#ID_сделки ваш ответ`\n"
                "Например: `#4 Добрый день, сейчас подберём`",
                parse_mode="Markdown"
            )
            return

    # Ищем клиента
    client_chat_id, client_data = find_client_by_deal(deal_id)
    if client_chat_id is None:
        await message.answer(f"⚠ К сделке #{deal_id} не привязан Telegram-чат.")
        return

    # Отправляем клиенту
    try:
        await bot.send_message(
            client_chat_id,
            f"📧 *Менеджер* (сделка LD-{deal_id}):\n{text}",
            parse_mode="Markdown"
        )
        add_to_timeline(deal_id, f"📤 Менеджер → клиенту: {text}")
        await message.answer(f"✅ Отправлено клиенту (сделка #{deal_id})")
    except Exception as e:
        await message.answer(f"⚠ Ошибка: {e}")

# ── Inline-кнопки ──

@dp.callback_query(lambda c: c.data.startswith("reply_"))
async def callback_reply(callback: types.CallbackQuery):
    deal_id = int(callback.data.split("_")[1])
    client_chat_id, client_data = find_client_by_deal(deal_id)

    if client_chat_id is None:
        await callback.answer("Клиент не привязан к этой сделке", show_alert=True)
        return

    await callback.message.answer(
        f"💬 Ответ клиенту по сделке *#{deal_id}*:\n"
        f"Клиент: {client_data.get('name', '?')}\n"
        f"Просто напишите сообщение (без #ID) — и оно уйдёт клиенту.\n"
        f"Или напишите `#{deal_id} ваш текст`",
        parse_mode="Markdown"
    )
    await callback.answer()

# ═══════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════

async def main():
    logger.info("=" * 50)
    logger.info("Telegram-бот для сделок Bitrix24 v2.0")
    logger.info("Менеджер: %s", MANAGER_CHAT_ID)
    logger.info("=" * 50)

    try:
        await bot.send_message(MANAGER_CHAT_ID, "🤖 Бот v2.0 запущен. Создаю сделки, веду диалоги.")
    except Exception as e:
        logger.error("Стартовое сообщение: %s", e)

    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
