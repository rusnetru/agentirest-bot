#!/usr/bin/env python3
"""
email_tracker.py — отслеживание почты менеджера через Mail.tm API
и привязка писем к лидам Bitrix24 по хеш-тегу LD-{ID} в теме.

API Mail.tm (временная почта):
  POST /token  — получить JWT
  GET  /messages — список писем
  GET  /messages/{id} — конкретное письмо

Bitrix24:
  crm.lead.get + crm.timeline.comment.add
"""

import json
import logging
import os
import re
import sys
from datetime import datetime

import requests

# ═══════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════

EMAIL_ADDRESS = "sapphirepayable@web-library.net"
EMAIL_PASSWORD = "v`J1_86'mH"

MAILTM_BASE = "https://api.mail.tm"
BITRIX24_WEBHOOK = "https://b24-ufslqf.bitrix24.ru/rest/1/wwaict4vpjhiku1s/"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "email_tracking_log.json")
PROCESSED_FILE = os.path.join(SCRIPT_DIR, "email_processed_ids.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("email_tracker")

# ═══════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════

def get_token():
    """Получить JWT токен Mail.tm."""
    r = requests.post(
        f"{MAILTM_BASE}/token",
        json={"address": EMAIL_ADDRESS, "password": EMAIL_PASSWORD},
        timeout=15,
    )
    data = r.json()
    return data.get("token"), data.get("id")


def get_messages(token):
    """Получить список всех сообщений."""
    r = requests.get(
        f"{MAILTM_BASE}/messages",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    data = r.json()
    if "hydra:member" in data:
        return data["hydra:member"]
    return []


def get_message(token, msg_id):
    """Получить полное сообщение по ID."""
    r = requests.get(
        f"{MAILTM_BASE}/messages/{msg_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if r.status_code == 200:
        return r.json()
    return None


def load_processed_ids():
    """Загрузить список уже обработанных ID писем."""
    if os.path.exists(PROCESSED_FILE):
        try:
            with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except:
            return set()
    return set()


def save_processed_ids(ids):
    """Сохранить список обработанных ID."""
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ids), f)


def call_bitrix(method, params=None):
    """Вызов Bitrix24 REST API."""
    url = BITRIX24_WEBHOOK + method
    try:
        r = requests.post(url, json=params or {}, timeout=30)
        result = r.json()
        if "error" in result:
            logger.error("Bitrix24 [%s]: %s — %s", method, result["error"], result.get("error_description", ""))
        return result
    except Exception as e:
        logger.error("Ошибка вызова Bitrix24 [%s]: %s", method, e)
        return {"error": str(e)}


def find_lead_by_id(lead_id):
    """Поиск лида по ID."""
    result = call_bitrix("crm.lead.get", {"ID": lead_id})
    if "error" not in result and result.get("result"):
        return result["result"]
    return None


def add_comment_to_lead(lead_id, comment_text):
    """Добавить комментарий в таймлайн лида."""
    result = call_bitrix("crm.timeline.comment.add", {
        "fields": {
            "ENTITY_ID": lead_id,
            "ENTITY_TYPE": "lead",
            "COMMENT": comment_text,
        }
    })
    return "error" not in result


def update_lead_email_date(lead_id, date_str):
    """Обновить UF_CRM_LEAD_LAST_EMAIL_DATE."""
    result = call_bitrix("crm.lead.update", {
        "ID": lead_id,
        "fields": {"UF_CRM_LEAD_LAST_EMAIL_DATE": date_str},
    })
    if "error" not in result:
        return True
    if "NOT_FOUND" in str(result.get("error", "")):
        logger.warning("Поле UF_CRM_LEAD_LAST_EMAIL_DATE не найдено. Пропускаем.")
    return False


def append_log(entry):
    """Добавить запись в лог."""
    logs = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except:
            logs = []
    logs.append(entry)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════
# ОСНОВНАЯ ФУНКЦИЯ
# ═══════════════════════════════════════════

def process_emails():
    """Основная функция: проверить почту через Mail.tm API, найти LD-{ID}, привязать к лидам."""

    logger.info("=" * 60)
    logger.info("Запуск email_tracker.py (Mail.tm API)")

    # 1. Токен
    logger.info("Получение токена Mail.tm...")
    token, account_id = get_token()
    if not token:
        logger.error("Не удалось получить токен Mail.tm")
        append_log({"timestamp": datetime.now().isoformat(), "status": "error", "error": "Token failed"})
        sys.exit(1)
    logger.info("Токен получен. Account ID: %s", account_id)

    # 2. Сообщения
    logger.info("Получение списка писем...")
    messages = get_messages(token)
    logger.info("Всего писем: %d", len(messages))

    if not messages:
        logger.info("Писем нет.")
        append_log({"timestamp": datetime.now().isoformat(), "status": "success", "processed": 0, "message": "Нет писем"})
        return

    # 3. Загружаем обработанные ID
    processed_ids = load_processed_ids()

    matched = 0
    processed = 0
    errors = []

    for msg in messages:
        msg_id = msg.get("id")
        subject = msg.get("subject", "") or "(без темы)"
        from_addr = msg.get("from", {}).get("address", "?")
        from_name = msg.get("from", {}).get("name", "")
        created = msg.get("createdAt", "")

        logger.info("--- Письмо %s ---", msg_id)
        logger.info("  От: %s <%s>", from_name, from_addr)
        logger.info("  Тема: %s", subject)

        # Пропускаем уже обработанные
        if msg_id in processed_ids:
            logger.info("  Уже обработано — пропускаем")
            continue

        # Ищем хеш-тег LD-{ID} в теме
        match = re.search(r"LD-(\d+)", subject)
        if not match:
            logger.info("  Хеш-тег LD-{ID} не найден — пропускаем")
            processed_ids.add(msg_id)
            continue

        lead_id = int(match.group(1))
        logger.info("  Найден хеш-тег LD-%d", lead_id)

        # Получаем полное сообщение для текста
        full_msg = get_message(token, msg_id)
        body_text = ""
        if full_msg:
            body_text = (full_msg.get("text") or full_msg.get("html") or "").strip()
            # Очистка HTML
            if full_msg.get("html") and not full_msg.get("text"):
                body_text = re.sub(r"<[^>]+>", " ", body_text)
                body_text = re.sub(r"\s+", " ", body_text).strip()
        body_preview = body_text[:500] if body_text else "(нет текста)"

        # Ищем лид
        lead = find_lead_by_id(lead_id)
        if not lead:
            logger.warning("  Лид LD-%d не найден в Bitrix24", lead_id)
            errors.append({"msg_id": msg_id, "lead_id": lead_id, "error": "Лид не найден"})
            processed_ids.add(msg_id)
            continue

        lead_title = lead.get("TITLE", f"Лид #{lead_id}")
        logger.info("  Лид: %s", lead_title)

        # Добавляем в таймлайн
        comment = (
            f"📧 Письмо от {from_name} <{from_addr}>: {subject}\n\n"
            f"{body_preview}"
        )
        if add_comment_to_lead(lead_id, comment):
            logger.info("  ✓ Добавлено в таймлайн лида %d", lead_id)
        else:
            logger.warning("  ✘ Не удалось добавить комментарий")
            errors.append({"msg_id": msg_id, "lead_id": lead_id, "error": "Комментарий не добавлен"})

        # Обновляем дату последнего письма
        today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        update_lead_email_date(lead_id, today)

        # Лог
        append_log({
            "timestamp": datetime.now().isoformat(),
            "status": "success",
            "msg_id": msg_id,
            "from": f"{from_name} <{from_addr}>",
            "subject": subject,
            "date": created,
            "lead_id": lead_id,
            "lead_title": lead_title,
            "action": "comment_added",
        })

        matched += 1
        processed_ids.add(msg_id)

    # 4. Сохраняем обработанные
    save_processed_ids(processed_ids)

    # 5. Итоги
    logger.info("=" * 60)
    logger.info("Обработка завершена.")
    logger.info("  Всего писем:         %d", len(messages))
    logger.info("  Привязано к лидам:   %d", matched)
    logger.info("  Ошибок:              %d", len(errors))

    append_log({
        "timestamp": datetime.now().isoformat(),
        "status": "completed",
        "total": len(messages),
        "matched_to_leads": matched,
        "errors": len(errors),
    })


if __name__ == "__main__":
    try:
        process_emails()
    except KeyboardInterrupt:
        logger.info("Прервано пользователем")
        sys.exit(1)
    except Exception as e:
        logger.critical("Критическая ошибка: %s", e)
        append_log({"timestamp": datetime.now().isoformat(), "status": "critical_error", "error": str(e)})
        sys.exit(1)
