#!/usr/bin/env python3
"""
Bitrix24 Lead Processing Script
================================
Обрабатывает входящие лиды без классификации:
- Классифицирует по заголовку через простые правила
- Создаёт/привязывает контакты и компании
- Обновляет лиды с UF-полями (классификация, хеш-тег, контакт, компания)
- Логирует каждый шаг в JSON-файл

Запуск:     python process_leads.py
Cron:       каждые 5-15 минут
Webhook:    https://b24-ufslqf.bitrix24.ru/rest/1/wwaict4vpjhiku1s/
Менеджер:   Иван Иванов, rusnetru@yahoo.com
"""

import requests
import json
import os
import sys
import re
from datetime import datetime
from email.utils import parseaddr

# === НАСТРОЙКИ ===
WEBHOOK = "https://b24-ufslqf.bitrix24.ru/rest/1/wwaict4vpjhiku1s/"
MANAGER_NAME = "Иван Иванов"
MANAGER_EMAIL = "rusnetru@yahoo.com"

# Путь к лог-файлу (рядом со скриптом)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "lead_processing_log.json")

# === ПРАВИЛА КЛАССИФИКАЦИИ ===
# Порядок важен: первое совпадение определяет категорию
CLASSIFICATION_RULES = [
    (r'(?i)\b(аналог|замена|подобрать|аналогичный|подбор)\b', 'Подбор аналогов'),
    (r'(?i)\b(тз|спецификаци[яю]|расчёт|расчет|посчитать|посчитайте|рассчитать|техническое\s*задание)\b', 'Расчёт ТЗ'),
]


def extract_domain(email):
    """Извлечь домен из email-адреса."""
    _, addr = parseaddr(email)
    if not addr or '@' not in addr:
        return None
    return addr.split('@')[1].lower()


def classify_lead(title):
    """
    Классифицировать лид по заголовку через простые правила.
    Возвращает: 'Подбор аналогов', 'Расчёт ТЗ' или 'Консультация'.
    """
    for pattern, category in CLASSIFICATION_RULES:
        if re.search(pattern, title):
            return category
    return 'Консультация'


def api_call(method, params=None, timeout=30):
    """Выполнить вызов Bitrix24 REST API с обработкой ошибок."""
    url = f"{WEBHOOK}{method}"
    try:
        resp = requests.post(url, json=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        return {'error': f'Timeout ({timeout}s)', 'result': None}
    except requests.exceptions.RequestException as e:
        return {'error': str(e), 'result': None}


def load_log():
    """Загрузить существующий лог или вернуть пустой список."""
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        return []
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def append_log(entry):
    """Добавить запись в JSON-лог (атомарно)."""
    log_data = load_log()
    log_data.append(entry)
    # Создаём директорию при необходимости
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)


def get_unclassified_leads():
    """
    Получить все лиды, у которых UF_CRM_LEAD_CLASSIFICATION пусто.
    Использует несколько стратегий фильтрации для надёжности.
    """
    all_leads = []
    strategies = [
        # Стратегия 1: '>' оператор — в Bitrix24 означает "заполнено",
        # а '!' отрицание. Ищем где UF_CRM_LEAD_CLASSIFICATION = '' (пусто)
        {'filter': {'UF_CRM_LEAD_CLASSIFICATION': ''}},
        # Стратегия 2: получить ВСЕ лиды и отфильтровать на стороне клиента
        {'filter': {}},
    ]

    for strategy in strategies:
        result = api_call('crm.lead.list', {
            'filter': strategy['filter'],
            'select': [
                'ID', 'TITLE', 'NAME', 'LAST_NAME', 'EMAIL',
                'CONTACT_ID', 'COMPANY_ID',
                'UF_CRM_LEAD_CLASSIFICATION', 'UF_CRM_LEAD_HASHTAG',
                'ASSIGNED_BY_ID', 'SOURCE_DESCRIPTION', 'COMMENTS'
            ]
        })

        if 'error' in result:
            continue

        leads = result.get('result', [])
        if leads:
            if strategy['filter'] == {}:
                # Для стратегии 2 — фильтруем на клиенте
                unclassified = [
                    l for l in leads
                    if not l.get('UF_CRM_LEAD_CLASSIFICATION')
                ]
                return unclassified
            return leads

    # Если ничего не сработало — последняя попытка: получить все лиды
    result = api_call('crm.lead.list', {
        'filter': {},
        'select': [
            'ID', 'TITLE', 'NAME', 'LAST_NAME', 'EMAIL',
            'CONTACT_ID', 'COMPANY_ID',
            'UF_CRM_LEAD_CLASSIFICATION', 'UF_CRM_LEAD_HASHTAG',
        ],
    })
    if 'error' not in result:
        all_leads = result.get('result', [])
        return [
            l for l in all_leads
            if not l.get('UF_CRM_LEAD_CLASSIFICATION')
        ]

    return []


def process_leads():
    """Основная функция: получить лиды, классифицировать, создать контакты/компании."""
    start_time = datetime.now()

    print("=" * 60)
    print("  Bitrix24 Lead Processing")
    print("=" * 60)
    print(f"  Started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Webhook: {WEBHOOK}")
    print()

    # --- 1. Получить неклассифицированные лиды ---
    print("[1] Fetching unclassified leads...")
    leads = get_unclassified_leads()

    if leads is None:
        print("  ERROR: Failed to fetch leads from API (timeout)")
        append_log({
            'type': 'run',
            'status': 'error',
            'error': 'Failed to fetch leads from API (timeout)',
            'timestamp': datetime.now().isoformat()
        })
        return

    print(f"  Found {len(leads)} unclassified lead(s)")

    if not leads:
        print("\n  No leads to process. Exiting.")
        append_log({
            'type': 'run',
            'status': 'completed',
            'leads_found': 0,
            'leads_processed': 0,
            'contacts_created': 0,
            'contacts_found': 0,
            'companies_created': 0,
            'companies_found': 0,
            'timestamp': datetime.now().isoformat()
        })
        return

    # --- 2. Обработать каждый лид ---
    print(f"\n[2] Processing {len(leads)} lead(s)...")
    print()

    stats = {
        'leads_processed': 0,
        'leads_skipped': 0,
        'contacts_created': 0,
        'contacts_found': 0,
        'companies_created': 0,
        'companies_found': 0,
        'errors': [],
    }

    for i, lead in enumerate(leads, 1):
        lead_id = lead.get('ID')
        title = lead.get('TITLE', '') or ''
        name = lead.get('NAME', '') or ''
        last_name = lead.get('LAST_NAME', '') or ''
        emails = lead.get('EMAIL', []) or []

        print(f"  ─── Lead #{i}: ID={lead_id} ───")
        print(f"  Title: {title[:80]}{'…' if len(title) > 80 else ''}")

        lead_log = {
            'type': 'lead',
            'lead_id': lead_id,
            'title': title,
            'name': name,
            'timestamp': datetime.now().isoformat()
        }

        # --- 2a. Извлечь email ---
        email = None
        if isinstance(emails, list) and len(emails) > 0:
            email = (emails[0].get('VALUE') or '').strip()

        if not email:
            print(f"  ⚠ No email — skipping lead")
            stats['leads_skipped'] += 1
            lead_log['status'] = 'skipped'
            lead_log['reason'] = 'no_email'
            append_log(lead_log)
            continue

        print(f"  Email: {email}")
        lead_log['email'] = email

        # --- 2b. Домен ---
        domain = extract_domain(email)
        if not domain:
            print(f"  ⚠ Invalid email '{email}' — skipping lead")
            stats['leads_skipped'] += 1
            lead_log['status'] = 'skipped'
            lead_log['reason'] = f'invalid_email: {email}'
            append_log(lead_log)
            continue

        print(f"  Domain: {domain}")
        lead_log['domain'] = domain

        # --- 2c. Классификация ---
        classification = classify_lead(title)
        print(f"  Classification: {classification}")
        lead_log['classification'] = classification

        # --- 2d. Контакт (найти или создать) ---
        contact_id = None
        contact_action = None

        # Поиск по email (точное совпадение)
        contact_search = api_call('crm.contact.list', {
            'filter': {'=EMAIL': email},
            'select': ['ID', 'NAME', 'LAST_NAME', 'COMPANY_ID']
        })

        if ('error' not in contact_search
                and contact_search.get('result')
                and len(contact_search['result']) > 0):
            contact_id = contact_search['result'][0]['ID']
            contact_action = 'found'
            stats['contacts_found'] += 1
            print(f"  Contact found: ID={contact_id}")
        else:
            # Создаём контакт
            # Разбиваем имя на имя/фамилию
            contact_source = name or email.split('@')[0]
            parts = contact_source.replace('.', ' ').split()
            c_name = parts[0] if parts else contact_source
            c_last = ' '.join(parts[1:]) if len(parts) > 1 else ''

            contact_result = api_call('crm.contact.add', {
                'fields': {
                    'NAME': c_name,
                    'LAST_NAME': c_last,
                    'EMAIL': [{'VALUE': email, 'VALUE_TYPE': 'WORK'}],
                    'OPENED': 'Y',
                    'ASSIGNED_BY_ID': 1,
                    'SOURCE_ID': 'WEBFORM',
                }
            })

            if ('error' not in contact_result
                    and contact_result.get('result')):
                contact_id = contact_result['result']
                contact_action = 'created'
                stats['contacts_created'] += 1
                print(f"  Contact created: ID={contact_id}")
            else:
                err = contact_result.get('error', 'unknown error')
                print(f"  ⚠ Failed to create contact: {err}")
                stats['errors'].append(f'Lead {lead_id}: contact create failed — {err}')

        lead_log['contact_id'] = contact_id
        lead_log['contact_action'] = contact_action

        # --- 2e. Компания (найти или создать) ---
        company_id = None
        company_action = None

        # Поиск компании, содержащей домен в названии
        company_search = api_call('crm.company.list', {
            'filter': {'%TITLE': domain},
            'select': ['ID', 'TITLE']
        })

        if ('error' not in company_search
                and company_search.get('result')
                and len(company_search['result']) > 0):
            company_id = company_search['result'][0]['ID']
            company_action = 'found'
            stats['companies_found'] += 1
            comp_title = company_search['result'][0].get('TITLE', '')
            print(f"  Company found: ID={company_id}, Title='{comp_title}'")
        else:
            # Создаём компанию
            domain_name = domain.split('.')[0].capitalize()
            company_title = f"{domain_name} ({domain})"

            company_result = api_call('crm.company.add', {
                'fields': {
                    'TITLE': company_title,
                    'EMAIL': [{'VALUE': email, 'VALUE_TYPE': 'WORK'}],
                    'OPENED': 'Y',
                    'ASSIGNED_BY_ID': 1,
                }
            })

            if ('error' not in company_result
                    and company_result.get('result')):
                company_id = company_result['result']
                company_action = 'created'
                stats['companies_created'] += 1
                print(f"  Company created: ID={company_id}, Title='{company_title}'")
            else:
                err = company_result.get('error', 'unknown error')
                print(f"  ⚠ Failed to create company: {err}")
                stats['errors'].append(f'Lead {lead_id}: company create failed — {err}')

        lead_log['company_id'] = company_id
        lead_log['company_action'] = company_action

        # --- 2f. Привязать контакт к компании ---
        if contact_id and company_id:
            link = api_call('crm.contact.update', {
                'id': contact_id,
                'fields': {'COMPANY_ID': company_id}
            })
            if 'error' not in link:
                print(f"  → Contact {contact_id} linked to Company {company_id}")
            else:
                print(f"  ⚠ Could not link contact to company: {link.get('error')}")

        # --- 2f2. Назначить ответственного менеджера компании на лид ---
        if company_id:
            company_info = api_call('crm.company.get', {'id': company_id})
            if 'error' not in company_info and company_info.get('result'):
                assigned_id = company_info['result'].get('ASSIGNED_BY_ID')
                if assigned_id and str(assigned_id) != '1':
                    # Назначаем менеджера компании на лид
                    assign_result = api_call('crm.lead.update', {
                        'id': lead_id,
                        'fields': {'ASSIGNED_BY_ID': assigned_id}
                    })
                    if 'error' not in assign_result:
                        print(f"  → Manager ID={assigned_id} assigned to lead (from company)")
                        lead_log['assigned_manager_id'] = assigned_id
                    else:
                        print(f"  ⚠ Could not assign manager: {assign_result.get('error')}")
                else:
                    print(f"  → Company has no dedicated manager (ASSIGNED_BY_ID={assigned_id})")

        # --- 2g. Хеш-тег ---
        hashtag = f"LD-{lead_id}"
        print(f"  Hashtag: {hashtag}")
        lead_log['hashtag'] = hashtag

        # --- 2h. Обновить лид ---
        update_fields = {
            'UF_CRM_LEAD_CLASSIFICATION': classification,
            'UF_CRM_LEAD_HASHTAG': hashtag,
            'UF_CRM_LEAD_SOURCE_EMAIL': email,
        }
        if contact_id:
            update_fields['CONTACT_ID'] = contact_id
        if company_id:
            update_fields['COMPANY_ID'] = company_id

        update_result = api_call('crm.lead.update', {
            'id': lead_id,
            'fields': update_fields
        })

        if 'error' not in update_result:
            print(f"  ✓ Lead {lead_id} updated successfully")
            stats['leads_processed'] += 1
            lead_log['status'] = 'processed'
        else:
            err = update_result.get('error', 'unknown')
            print(f"  ⚠ Failed to update lead: {err}")
            stats['errors'].append(f'Lead {lead_id}: update failed — {err}')
            lead_log['status'] = 'update_failed'
            lead_log['update_error'] = err

        append_log(lead_log)
        print()

    # --- 3. Итоговый отчёт ---
    duration = (datetime.now() - start_time).total_seconds()

    print("=" * 60)
    print("  REPORT")
    print("=" * 60)
    print(f"  Leads found (unclassified):  {len(leads)}")
    print(f"  Leads successfully processed: {stats['leads_processed']}")
    print(f"  Leads skipped (no/invalid email): {stats['leads_skipped']}")
    print(f"  Contacts created:             {stats['contacts_created']}")
    print(f"  Contacts found (existing):     {stats['contacts_found']}")
    print(f"  Companies created:            {stats['companies_created']}")
    print(f"  Companies found (existing):    {stats['companies_found']}")
    if stats['errors']:
        print(f"  Errors ({len(stats['errors'])}):")
        for err in stats['errors'][:5]:
            print(f"    • {err}")
        if len(stats['errors']) > 5:
            print(f"    … and {len(stats['errors']) - 5} more")
    print(f"  Duration: {duration:.1f}s")
    print(f"  Log: {LOG_FILE}")
    print("=" * 60)

    # --- Лог запуска ---
    append_log({
        'type': 'run',
        'status': 'completed',
        'timestamp': datetime.now().isoformat(),
        'duration_seconds': duration,
        'leads_found': len(leads),
        'leads_processed': stats['leads_processed'],
        'leads_skipped': stats['leads_skipped'],
        'contacts_created': stats['contacts_created'],
        'contacts_found': stats['contacts_found'],
        'companies_created': stats['companies_created'],
        'companies_found': stats['companies_found'],
        'errors': stats['errors'],
    })


if __name__ == '__main__':
    try:
        process_leads()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        append_log({
            'type': 'run',
            'status': 'fatal_error',
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        })
        sys.exit(1)
