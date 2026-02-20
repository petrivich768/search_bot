import os
import re
import time
import aiohttp
import json
import asyncio
import hashlib
import urllib.parse
import phonenumbers
from phonenumbers import carrier, geocoder, timezone
from bs4 import BeautifulSoup
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler, ConversationHandler
)

# Загружаем переменные из .env
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
LEAKCHECK_KEY = os.getenv('LEAKCHECK_KEY', '')  # если не задан, будет пустая строка

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан! Создайте файл .env и укажите токен.")

ADMIN_ID = 8359674526

# Состояния для ConversationHandler
(CHOOSING, TYPING_NICK, TYPING_TG_USERNAME, TYPING_IP,
 TYPING_GITHUB_USERNAME, TYPING_EMAIL, TYPING_DOMAIN, TYPING_PHONE,
 TYPING_ADMIN_USER_ID, TYPING_ADMIN_AMOUNT) = range(10)

# ---------- Хранилище лимитов и защиты ----------
user_limits = {}
user_state = {}
last_request_time = {}
last_notify_time = {}

MAX_REQUESTS_PER_DAY = 3
MIN_INTERVAL_SECONDS = 2
MAX_INPUT_LENGTH = 500

# ---------- Отправка уведомлений админу ----------
def safe_send_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, error_text: str):
    now = time.time()
    last = last_notify_time.get('admin', 0)
    if now - last > 60:
        last_notify_time['admin'] = now
        try:
            context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Ошибка в боте:\n{error_text[:500]}")
        except:
            pass

def check_and_increment_limit(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    today = datetime.now().date().isoformat()
    data = user_limits.get(user_id)
    if data is None:
        user_limits[user_id] = {"date": today, "count": 1, "bonus": 0}
        return True
    else:
        if data["bonus"] > 0:
            data["bonus"] -= 1
            return True
        else:
            if data["date"] == today:
                if data["count"] < MAX_REQUESTS_PER_DAY:
                    data["count"] += 1
                    return True
                else:
                    return False
            else:
                data["date"] = today
                data["count"] = 1
                return True

# ---------- Вспомогательные функции проверки ----------
def is_telegram_username(text: str):
    if text.startswith('@'):
        return text[1:]
    return None

def is_ip(text: str):
    pattern = r'^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'
    return re.match(pattern, text) is not None

def is_email(text: str):
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, text) is not None

def is_phone(text: str):
    cleaned = re.sub(r'[^\d+]', '', text)
    if cleaned.startswith('+'):
        return cleaned[1:].isdigit() and 8 <= len(cleaned[1:]) <= 15
    else:
        return cleaned.isdigit() and 8 <= len(cleaned) <= 15

# ---------- Поиск по нику (соцсети) ----------
async def check_social_media(nick: str):
    sites = {
        "Twitter": f"https://twitter.com/{nick}",
        "Instagram": f"https://instagram.com/{nick}",
        "TikTok": f"https://tiktok.com/@{nick}",
        "GitHub": f"https://github.com/{nick}",
        "Reddit": f"https://reddit.com/user/{nick}",
        "Pinterest": f"https://pinterest.com/{nick}",
        "Twitch": f"https://twitch.tv/{nick}",
        "YouTube": f"https://youtube.com/@{nick}",
        "Facebook": f"https://facebook.com/{nick}",
        "Telegram": f"https://t.me/{nick}",
        "VK": f"https://vk.com/{nick}",
        "Snapchat": f"https://snapchat.com/add/{nick}",
        "Tumblr": f"https://{nick}.tumblr.com",
        "Steam": f"https://steamcommunity.com/id/{nick}",
    }
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    found = []
    async with aiohttp.ClientSession() as session:
        for name, url in sites.items():
            try:
                async with session.head(url, headers=headers, allow_redirects=True, timeout=5) as resp:
                    if resp.status == 200:
                        found.append((name, url))
            except Exception:
                continue
    return found

# ---------- Получение Telegram ID ----------
async def get_telegram_id(username: str, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = await context.bot.get_chat(chat_id=f"@{username}")
        return chat.id, None
    except Exception as e:
        return None, str(e)

# ---------- Информация по IP ----------
async def get_ip_info(ip: str):
    url = f'http://ip-api.com/json/{ip}?fields=status,message,country,regionName,city,zip,lat,lon,isp,org,as,query'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if data.get('status') == 'success':
                return data, None
            else:
                return None, data.get('message', 'Unknown error')

def format_ip_info(data: dict) -> str:
    lines = [
        f"IP: {data.get('query')}",
        f"Страна: {data.get('country')}",
        f"Регион: {data.get('regionName')}",
        f"Город: {data.get('city')}",
        f"Почтовый индекс: {data.get('zip')}",
        f"Координаты: {data.get('lat')}, {data.get('lon')}",
        f"Провайдер: {data.get('isp')}",
        f"Организация: {data.get('org')}",
        f"AS: {data.get('as')}"
    ]
    return '\n'.join(lines)

# ---------- Поиск по GitHub (по username) ----------
async def github_find_info_by_username(username: str):
    result = {}
    output_lines = []

    url = f'https://api.github.com/users/{username}'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                fields = ['login', 'id', 'avatar_url', 'name', 'blog', 'location',
                          'twitter_username', 'company', 'bio',
                          'public_repos', 'followers', 'following', 'created_at', 'updated_at']
                for f in fields:
                    if data.get(f):
                        result[f] = data[f]
                        output_lines.append(f'[+] {f} : {data[f]}')
                result['public_gists'] = f'https://gist.github.com/{username}'
                output_lines.append(f'[+] public_gists : https://gist.github.com/{username}')
            else:
                return None, "Пользователь не найден или ошибка API"

    gpg_url = f'https://github.com/{username}.gpg'
    ssh_url = f'https://github.com/{username}.keys'
    async with aiohttp.ClientSession() as session:
        async with session.get(gpg_url) as resp:
            if resp.status == 200:
                gpg_text = await resp.text()
                if "hasn't uploaded any GPG keys" not in gpg_text:
                    result['GPG_keys'] = gpg_url
                    output_lines.append(f'[+] GPG_keys : {gpg_url}')
        async with session.get(ssh_url) as resp:
            if resp.status == 200 and await resp.text():
                result['SSH_keys'] = ssh_url
                output_lines.append(f'[+] SSH_keys : {ssh_url}')

    return result, output_lines

# ---------- Основные API для email/domain ----------
HUDSON_URL = "https://cavalier.hudsonrock.com/api/json/v2/osint-tools"
PROXYNOVA_URL = "https://api.proxynova.com/comb"
PSBDMP_URL = "https://psbdmp.ws/api/search"

async def search_hudson_email(session, email):
    url = f"{HUDSON_URL}/search-by-email"
    params = {'email': email}
    return await _make_request(session, url, params, "Hudson Rock")

async def search_hudson_domain(session, domain):
    url = f"{HUDSON_URL}/search-by-domain"
    params = {'domain': domain}
    return await _make_request(session, url, params, "Hudson Rock")

async def search_leakcheck(session, query):
    url = "https://leakcheck.net/api/public"
    params = {'key': LEAKCHECK_KEY, 'check': query}
    return await _make_request(session, url, params, "LeakCheck")

async def search_proxynova_email(session, email):
    import urllib.parse
    encoded = urllib.parse.quote(email)
    url = f"{PROXYNOVA_URL}?query={encoded}&start=0&limit=100"
    return await _make_request(session, url, {}, "ProxyNova")

async def search_psbdmp_email(session, email):
    url = f"{PSBDMP_URL}/email/{email}"
    return await _make_request(session, url, {}, "PSBDmp")

async def search_psbdmp_domain(session, domain):
    url = f"{PSBDMP_URL}/domain/{domain}"
    return await _make_request(session, url, {}, "PSBDmp")

async def _make_request(session, url, params, source):
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with session.get(url, params=params, timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                return {"error": f"{source}: HTTP {resp.status}"}
    except asyncio.TimeoutError:
        return {"error": f"{source}: Таймаут запроса"}
    except Exception as e:
        return {"error": f"{source}: {str(e)}"}

# ---------- Модули из EYES ----------
async def search_duolingo(session, email):
    url = "https://www.duolingo.com/2017-06-30/users"
    params = {'email': email}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        async with session.get(url, params=params, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('users') and len(data['users']) > 0:
                    user = data['users'][0]
                    lines = [f"✅ Duolingo"]
                    lines.append(f"  └──Username: {user.get('username', '?')}")
                    if user.get('bio'):
                        lines.append(f"     Bio: {user['bio']}")
                    if user.get('totalXp'):
                        lines.append(f"     Total XP: {user['totalXp']}")
                    if user.get('courses') and len(user['courses']) > 0:
                        lines.append(f"     From: {user['courses'][0].get('fromLanguage', '?')}")
                    return "\n".join(lines)
    except Exception:
        pass
    return None

async def search_gravatar(session, email):
    email_hash = hashlib.md5(email.lower().encode()).hexdigest()
    url = f"https://en.gravatar.com/{email_hash}.json"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('entry') and len(data['entry']) > 0:
                    display_name = data['entry'][0].get('displayName')
                    if display_name:
                        return f"✅ Gravatar\n  └──Name: {display_name}"
                    else:
                        return "✅ Gravatar"
    except Exception:
        pass
    return None

async def search_imgur(session, email):
    url = "https://imgur.com/signin/ajax_email_available"
    headers = {'User-Agent': 'Mozilla/5.0'}
    data = {'email': email}
    try:
        async with session.post(url, headers=headers, data=data, timeout=10) as resp:
            if resp.status == 200:
                text = await resp.text()
                if '"data":{"available":false}' in text:
                    return "✅ Imgur"
    except Exception:
        pass
    return None

async def search_mailru(session, email):
    url = f"https://account.mail.ru/api/v1/user/exists?email={email}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('body', {}).get('exists') is True:
                    return "✅ Mail.ru"
    except Exception:
        pass
    return None

async def search_protonmail(session, email):
    url = f"https://api.protonmail.ch/pks/lookup?op=index&search={email}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                text = await resp.text()
                if "info:1:1" in text:
                    match = re.search(r'2048:(.*?)::', text) or re.search(r'4096:(.*?)::', text)
                    if match:
                        timestamp = int(match.group(1))
                        date = datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
                        return f"✅ ProtonMail (PGP created: {date} UTC)"
                    else:
                        return "✅ ProtonMail"
    except Exception:
        pass
    return None

async def search_bitmoji(session, email):
    url = "https://bitmoji.api.snapchat.com/api/user/find"
    headers = {'User-Agent': 'Mozilla/5.0'}
    data = {'email': email}
    try:
        async with session.post(url, headers=headers, data=data, timeout=10) as resp:
            if resp.status == 200:
                text = await resp.text()
                if '{"account_type":"snapchat"}' in text:
                    return "✅ Bitmoji (Snapchat)"
    except Exception:
        pass
    return None

async def search_instagram(session, email):
    url = f"https://www.instagram.com/web/search/topsearch/?context=blended&query={email}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                users = data.get('users', [])
                if users:
                    user_info = users[0].get('user', {})
                    username = user_info.get('username')
                    pic = user_info.get('profile_pic_url')
                    if username:
                        return f"✅ Instagram\n  └──Username: {username}\n  └──Profile pic: {pic}"
    except Exception:
        pass
    return None

async def search_twitter(session, email):
    url = f"https://api.twitter.com/i/users/email_available.json?email={email}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('taken') is True:
                    return "✅ X (Twitter)"
    except Exception:
        pass
    return None

async def search_github_email(session, email):
    url = f"https://api.github.com/search/users?q={email}+in:email"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('total_count', 0) > 0:
                    items = data.get('items', [])
                    if items:
                        login = items[0].get('login')
                        avatar = items[0].get('avatar_url')
                        return f"✅ GitHub\n  └──Username: {login}\n  └──Avatar: {avatar}"
    except Exception:
        pass
    return None

# ---------- Форматирование результатов ----------
def format_hudson_standard(data, search_type, query):
    if not isinstance(data, dict):
        return "❌ Информация не найдена"
    lines = []
    if "error" in data:
        return "❌ Информация не найдена"
    if "message" in data:
        lines.append(f"ℹ️ {data['message']}")
    if "total_corporate_services" in data or "total_user_services" in data:
        corp = data.get('total_corporate_services', 0)
        user = data.get('total_user_services', 0)
        lines.append(f"Статистика: корп.сервисов {corp}, польз.сервисов {user}")
    if "stealers" in data and data["stealers"]:
        lines.append(f"Найдено зараженных устройств: {len(data['stealers'])}")
        for i, stealer in enumerate(data["stealers"][:3], 1):
            date = stealer.get('date_compromised', '?')
            ip = stealer.get('ip', '?')
            os = stealer.get('operating_system', '?')
            lines.append(f"Устройство {i}: {date}, IP {ip}, OS {os}")
            if stealer.get("top_logins"):
                logins = stealer["top_logins"][:3]
                lines.append(f"  Логины: {', '.join(logins)}")
    else:
        lines.append("Информация о заражениях не найдена")
    if not lines:
        return "❌ Информация не найдена"
    return "\n".join(lines)

def format_hudson_domain(data, query):
    if not isinstance(data, dict):
        return "❌ Информация не найдена"
    lines = []
    if "error" in data:
        return "❌ Информация не найдена"
    if "total" in data:
        lines.append(f"Всего записей: {data.get('total', 0)}")
        lines.append(f"Сотрудников: {data.get('employees', 0)}")
        lines.append(f"Пользователей: {data.get('users', 0)}")
    if "data" in data:
        d = data["data"]
        if d.get("employees_urls"):
            lines.append(f"Найдено URL сотрудников: {len(d['employees_urls'])}")
        if d.get("clients_urls"):
            lines.append(f"Найдено URL клиентов: {len(d['clients_urls'])}")
    if not lines:
        return "❌ Информация не найдена"
    return "\n".join(lines)

def format_leakcheck(data, query):
    if not isinstance(data, dict):
        return "❌ Информация не найдена"
    if "error" in data:
        return "❌ Информация не найдена"
    if data.get('success'):
        found = data.get('found', 0)
        if found == 0:
            return "❌ Информация не найдена"
        lines = [f"✅ LeakCheck: найдено записей: {found}"]
        if data.get('sources'):
            sources = data['sources'][:10]
            lines.append("Источники утечек:")
            for s in sources:
                name = s.get('name', '?')
                date = s.get('date', '?')
                lines.append(f"• {name} ({date})")
        return "\n".join(lines)
    return "❌ Информация не найдена"

def format_proxynova(data, query):
    if not isinstance(data, dict):
        return "❌ Информация не найдена"
    if "error" in data:
        return "❌ Информация не найдена"
    proxies = []
    if 'lines' in data:
        proxies = data['lines']
    elif 'proxies' in data:
        proxies = data['proxies']
    elif 'results' in data:
        proxies = data['results']
    if proxies:
        lines = [f"✅ ProxyNova: найдено записей: {len(proxies)}"]
        for p in proxies[:10]:
            lines.append(f"• {p}")
        return "\n".join(lines)
    return "❌ Информация не найдена"

def format_psbdmp(data, query, search_type):
    if not isinstance(data, list):
        return "❌ Информация не найдена"
    if data:
        lines = [f"✅ PSBDmp: найдено паст: {len(data)}"]
        for p in data[:10]:
            paste_id = p.get('id', '?')
            tags = p.get('tags', '?')
            lines.append(f"• ID: {paste_id} | Теги: {tags}")
        return "\n".join(lines)
    return "❌ Информация не найдена"

# ---------- Поиск по номеру телефона ----------
async def get_phone_info(phone: str):
    clean_phone = re.sub(r'[^0-9]', '', phone)
    if not clean_phone:
        return None, "❌ Некорректный номер"

    async with aiohttp.ClientSession() as session:
        tasks = [
            _local_scan(clean_phone),
            _htmlweb_scan(session, clean_phone),
            _phoneradar_scan(session, clean_phone),
            _avito_scan(session, clean_phone),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    local_data, htmlweb_data, phoneradar_data, avito_data = results[:4]

    lines = [f"📞 Результаты поиска по номеру {phone}\n"]

    if local_data and not isinstance(local_data, Exception):
        lines.append(f"Международный формат: {local_data.get('intl', 'Неизвестно')}")
        lines.append(f"Код страны: {local_data.get('country_code', 'Неизвестно')}")
        lines.append(f"Страна: {local_data.get('country', 'Неизвестно')}")
        lines.append(f"Оператор: {local_data.get('carrier', 'Не найдено')}")
        if 'timezones' in local_data and local_data['timezones']:
            tz_list = ', '.join(local_data['timezones'])
            lines.append(f"Часовые пояса: {tz_list}")
        lines.append("")

    if htmlweb_data and not isinstance(htmlweb_data, Exception):
        lines.append(f"Страна (HTMLWeb): {htmlweb_data.get('country', 'Неизвестно')}")
        lines.append(f"Код страны: {htmlweb_data.get('country_code', 'Неизвестно')}")
        if 'length' in htmlweb_data:
            lines.append(f"Длина номера: {htmlweb_data['length']}")
        if 'location' in htmlweb_data:
            lines.append(f"Локация: {htmlweb_data['location']}")
        if 'language' in htmlweb_data:
            lines.append(f"Язык: {htmlweb_data['language']}")
        if 'region' in htmlweb_data:
            lines.append(f"Область: {htmlweb_data['region']}")
        if 'district' in htmlweb_data:
            lines.append(f"Округ: {htmlweb_data['district']}")
        if 'capital' in htmlweb_data:
            lines.append(f"Столица: {htmlweb_data['capital']}")
        if 'capital_code' in htmlweb_data:
            lines.append(f"Код столицы: {htmlweb_data['capital_code']}")
        if 'city' in htmlweb_data:
            lines.append(f"Город: {htmlweb_data['city']}")
        if 'area' in htmlweb_data:
            lines.append(f"Район: {htmlweb_data['area']}")
        if 'operator' in htmlweb_data:
            lines.append(f"Оператор: {htmlweb_data['operator']}")
        if 'range' in htmlweb_data:
            lines.append(f"Диапазон номеров: {htmlweb_data['range']}")
        lines.append("")

    if phoneradar_data and not isinstance(phoneradar_data, Exception):
        if 'operator' in phoneradar_data:
            lines.append(f"Оператор (PhoneRadar): {phoneradar_data['operator']}")
        if 'region' in phoneradar_data:
            lines.append(f"Регион (PhoneRadar): {phoneradar_data['region']}")
        lines.append("")

    if avito_data and not isinstance(avito_data, Exception):
        lines.append(f"Avito объявлений: {avito_data.get('count', 0)}")
        lines.append("")

    lines.append("Социальные сети:")
    lines.append("├ Instagram: https://www.instagram.com/accounts/password/reset")
    lines.append("├ ВКонтакте: https://vk.com/restore")
    lines.append("├ Facebook: https://facebook.com/login/identify/?ctx=recover&ars=royal_blue_bar")
    lines.append("├ Twitter: https://twitter.com/account/begin_password_reset")
    lines.append("└ LinkedIn: https://linkedin.com/checkpoint/rp/request-password-reset-submit")
    lines.append("")

    lines.append("Мессенджеры:")
    lines.append(f"├ WhatsApp: https://api.whatsapp.com/send?phone={clean_phone}")
    lines.append(f"├ Viber: viber://add?number={clean_phone}")
    lines.append(f"└ Skype: skype:{clean_phone}?call")

    return "\n".join(lines), None

async def _local_scan(phone: str):
    try:
        parsed = phonenumbers.parse(phone, None)
        if not phonenumbers.is_valid_number(parsed):
            return None
        return {
            "intl": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
            "country_code": f"+{parsed.country_code}",
            "country": geocoder.country_name_for_number(parsed, "ru"),
            "carrier": carrier.name_for_number(parsed, "ru") or "Не найдено",
            "timezones": timezone.time_zones_for_number(parsed)
        }
    except:
        return None

async def _htmlweb_scan(session, phone: str):
    try:
        url = f"https://htmlweb.ru/geo/api.php?json&telcod={phone}"
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                try:
                    data = await resp.json()
                except json.JSONDecodeError:
                    return None
                if 'error' in data:
                    return None
                result = {}
                if 'country' in data:
                    result['country'] = data['country'].get('name', 'Неизвестно')
                    result['country_code'] = data['country'].get('iso', '')
                if '0' in data:
                    result['operator'] = data['0'].get('oper', '')
                    result['range'] = data['0'].get('range', '')
                if 'region' in data:
                    result['region'] = data['region'].get('name', '')
                    if 'okrug' in data['region']:
                        result['district'] = data['region']['okrug']
                if 'city' in data:
                    result['city'] = data['city'].get('name', '')
                if 'capital' in data:
                    result['capital'] = data['capital'].get('name', '')
                    if 'code' in data['capital']:
                        result['capital_code'] = data['capital']['code']
                result['length'] = data.get('length', '')
                result['location'] = data.get('location', '')
                result['language'] = data.get('language', '')
                return result
    except Exception:
        return None

async def _phoneradar_scan(session, phone: str):
    try:
        url = f"https://phoneradar.ru/phone/{phone}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                html = await resp.text()
                soup = BeautifulSoup(html, 'html.parser')
                result = {}
                info = soup.find('div', class_='phone-info')
                if info:
                    lines = info.get_text('\n').split('\n')
                    for line in lines:
                        if 'Оператор' in line:
                            result['operator'] = line.split(':')[-1].strip()
                        if 'Регион' in line:
                            result['region'] = line.split(':')[-1].strip()
                return result
    except Exception:
        return None

async def _avito_scan(session, phone: str):
    try:
        url = f"https://mirror.redlime.space/search_by_phone/{phone}"
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                html = await resp.text()
                soup = BeautifulSoup(html, 'html.parser')
                ads = soup.find_all('div', class_='item') if soup else []
                return {"count": len(ads)}
    except Exception:
        return None

# ---------- Профиль пользователя ----------
def get_profile_info(user_id: int) -> str:
    if user_id == ADMIN_ID:
        return "👑 **Администратор**\nУ вас нет ограничений на запросы."
    data = user_limits.get(user_id)
    if data is None:
        return f"📊 **Ваш профиль**\n• Использовано сегодня: 0 из {MAX_REQUESTS_PER_DAY}\n• Бонусных запросов: 0"
    else:
        today = datetime.now().date().isoformat()
        if data["date"] == today:
            used = data["count"]
        else:
            used = 0
        bonus = data["bonus"]
        return f"📊 **Ваш профиль**\n• Использовано сегодня: {used} из {MAX_REQUESTS_PER_DAY}\n• Бонусных запросов: {bonus}"

# ---------- Админ-команда: добавить бонусы ----------
async def add_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ У вас нет прав администратора.")
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("❌ Использование: /addbonus <id_пользователя> <количество>")
        return
    try:
        target_user_id = int(args[0])
        amount = int(args[1])
        if amount <= 0:
            await update.message.reply_text("❌ Количество должно быть положительным числом.")
            return
    except ValueError:
        await update.message.reply_text("❌ Неверный формат чисел.")
        return
    await apply_bonus(update, context, target_user_id, amount)

async def apply_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int, amount: int):
    if target_user_id not in user_limits:
        today = datetime.now().date().isoformat()
        user_limits[target_user_id] = {"date": today, "count": 0, "bonus": amount}
    else:
        user_limits[target_user_id]["bonus"] += amount
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"🎉 Вам добавлено +{amount} бонусных запросов!"
        )
        await update.message.reply_text(f"✅ Бонусы добавлены пользователю {target_user_id}. Уведомление отправлено.")
    except Exception:
        await update.message.reply_text(f"⚠️ Бонусы добавлены, но уведомление не отправлено.")

# ---------- Обработчики команд и кнопок ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        with open('anonimms.jpg', 'rb') as f:
            await update.message.reply_photo(
                photo=f,
                caption="Добро пожаловать в Телеграм-Бот поиска данных!\nВыбери действие:"
            )
    except FileNotFoundError:
        await update.message.reply_text(
            "Здравствуй! Я бот для поиска информации.\nВыбери действие:"
        )

    keyboard = [
        [InlineKeyboardButton("🔍 Поиск по нику", callback_data="nick")],
        [InlineKeyboardButton("🆔 Telegram ID по username", callback_data="tgid")],
        [InlineKeyboardButton("🌐 Информация по IP", callback_data="ip")],
        [InlineKeyboardButton("🐙 GitHub по username", callback_data="github_user")],
        [InlineKeyboardButton("📧 Поиск по email", callback_data="email")],
        [InlineKeyboardButton("🌍 Поиск по домену", callback_data="domain")],
        [InlineKeyboardButton("📞 Поиск по номеру телефона", callback_data="phone")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
    ]
    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("🔧 Админ: пополнить запросы", callback_data="admin_add_bonus")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Что хотите найти?", reply_markup=reply_markup)
    return CHOOSING

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    action = query.data

    user_state[user_id] = action

    if action == "nick":
        await query.edit_message_text("Введите ник (например, durov):")
        return TYPING_NICK
    elif action == "tgid":
        await query.edit_message_text("Введите @username (например, @durov):")
        return TYPING_TG_USERNAME
    elif action == "ip":
        await query.edit_message_text("Введите IP-адрес (например, 8.8.8.8):")
        return TYPING_IP
    elif action == "github_user":
        await query.edit_message_text("Введите username на GitHub (например, octocat):")
        return TYPING_GITHUB_USERNAME
    elif action == "email":
        await query.edit_message_text("Введите email для проверки утечек:")
        return TYPING_EMAIL
    elif action == "domain":
        await query.edit_message_text("Введите домен (например, example.com):")
        return TYPING_DOMAIN
    elif action == "phone":
        await query.edit_message_text("Введите номер телефона в международном формате (например, +79123456789 или 79123456789):")
        return TYPING_PHONE
    elif action == "profile":
        info = get_profile_info(user_id)
        await query.message.reply_text(info, parse_mode='Markdown')
        return await return_to_menu(update)
    elif action == "admin_add_bonus":
        if user_id != ADMIN_ID:
            await query.edit_message_text("⛔ У вас нет прав администратора.")
            return CHOOSING
        await query.edit_message_text("Введите ID пользователя (число), которому хотите добавить бонусы:")
        return TYPING_ADMIN_USER_ID
    else:
        await query.edit_message_text("Неизвестное действие.")
        return CHOOSING

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if len(text) > MAX_INPUT_LENGTH:
        await update.message.reply_text(f"❌ Слишком длинный запрос (макс. {MAX_INPUT_LENGTH} символов).")
        return await return_to_menu(update)

    if user_id != ADMIN_ID:
        now = time.time()
        last = last_request_time.get(user_id, 0)
        if now - last < MIN_INTERVAL_SECONDS:
            await update.message.reply_text(f"⏳ Слишком часто. Подождите {MIN_INTERVAL_SECONDS} секунды.")
            return await return_to_menu(update)
        last_request_time[user_id] = now

    action = user_state.get(user_id)

    if action is None:
        if is_telegram_username(text):
            action = "tgid"
        elif is_ip(text):
            action = "ip"
        elif is_email(text):
            action = "email"
        elif is_phone(text):
            action = "phone"
        else:
            action = "nick"

    if action not in ["admin_add_bonus"]:
        if not check_and_increment_limit(user_id):
            await update.message.reply_text(f"❌ Вы исчерпали дневной лимит ({MAX_REQUESTS_PER_DAY} запроса). Попробуйте завтра или используйте бонусы.")
            return await return_to_menu(update)

    try:
        if action == "nick":
            await update.message.reply_text(f"🔍 Ищу профили с ником '{text}'...")
            found = await check_social_media(text)
            if found:
                lines = [f"Найдены профили для '{text}':"]
                for name, url in found:
                    lines.append(f"• {name}: {url}")
                full = '\n'.join(lines)
                if len(full) <= 4096:
                    await update.message.reply_text(full)
                else:
                    parts = []
                    current = ""
                    for line in lines:
                        if len(current) + len(line) + 1 > 4096:
                            parts.append(current)
                            current = line
                        else:
                            if current:
                                current += "\n" + line
                            else:
                                current = line
                    if current:
                        parts.append(current)
                    for part in parts:
                        await update.message.reply_text(part)
            else:
                await update.message.reply_text("❌ Информация не найдена")

        elif action == "tgid":
            username = text.lstrip('@')
            await update.message.reply_text(f"⏳ Получаю ID для @{username}...")
            uid, err = await get_telegram_id(username, context)
            if err:
                await update.message.reply_text("❌ Информация не найдена")
            else:
                await update.message.reply_text(f"✅ ID пользователя @{username}: `{uid}`", parse_mode='Markdown')

        elif action == "ip":
            await update.message.reply_text(f"⏳ Получаю информацию об IP {text}...")
            data, err = await get_ip_info(text)
            if err:
                await update.message.reply_text("❌ Информация не найдена")
            else:
                info = format_ip_info(data)
                await update.message.reply_text(info)

        elif action == "github_user":
            await update.message.reply_text(f"⏳ Ищу информацию о пользователе GitHub '{text}'...")
            result, output = await github_find_info_by_username(text)
            if result is None:
                await update.message.reply_text("❌ Информация не найдена")
            else:
                for line in output:
                    await update.message.reply_text(line)

        elif action == "email":
            if not is_email(text):
                await update.message.reply_text("❌ Некорректный email. Попробуйте снова.")
                return TYPING_EMAIL
            await update.message.reply_text(f"⏳ Проверяю email {text}...")
            async with aiohttp.ClientSession() as session:
                tasks = [
                    search_hudson_email(session, text),
                    search_leakcheck(session, text),
                    search_proxynova_email(session, text),
                    search_psbdmp_email(session, text),
                    search_duolingo(session, text),
                    search_gravatar(session, text),
                    search_imgur(session, text),
                    search_mailru(session, text),
                    search_protonmail(session, text),
                    search_bitmoji(session, text),
                    search_instagram(session, text),
                    search_twitter(session, text),
                    search_github_email(session, text),
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

            hudson, leakcheck, proxynova, psbdmp, duolingo, gravatar, imgur, mailru, protonmail, bitmoji, instagram, twitter, github = results[:13]

            result_parts = []

            hudson_text = format_hudson_standard(hudson, "email", text)
            if hudson_text and "❌" not in hudson_text:
                result_parts.append(hudson_text)
            leakcheck_text = format_leakcheck(leakcheck, text)
            if leakcheck_text and "❌" not in leakcheck_text:
                result_parts.append(leakcheck_text)
            proxynova_text = format_proxynova(proxynova, text)
            if proxynova_text and "❌" not in proxynova_text:
                result_parts.append(proxynova_text)
            psbdmp_text = format_psbdmp(psbdmp, text, "email")
            if psbdmp_text and "❌" not in psbdmp_text:
                result_parts.append(psbdmp_text)

            for res in [duolingo, gravatar, imgur, mailru, protonmail, bitmoji, instagram, twitter, github]:
                if res and isinstance(res, str) and not res.startswith("❌"):
                    result_parts.append(res)

            pastebin_url = f"https://www.google.com/search?q=site:pastebin.com+{text}"
            result_parts.append(f"🔍 Pastebin: [поиск в Google]({pastebin_url})")

            if not result_parts:
                await update.message.reply_text("❌ Информация не найдена")
            else:
                await update.message.reply_text("📧 **Результаты поиска по email**", parse_mode='Markdown')
                for part in result_parts:
                    if len(part) <= 4096:
                        await update.message.reply_text(part, disable_web_page_preview=True)
                    else:
                        for i in range(0, len(part), 4096):
                            await update.message.reply_text(part[i:i+4096], disable_web_page_preview=True)

        elif action == "domain":
            await update.message.reply_text(f"⏳ Проверяю домен {text}...")
            async with aiohttp.ClientSession() as session:
                tasks = [
                    search_hudson_domain(session, text),
                    search_leakcheck(session, text),
                    search_psbdmp_domain(session, text)
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            hudson, leakcheck, psbdmp = results[:3]

            result_parts = []
            hudson_text = format_hudson_domain(hudson, text)
            if hudson_text and "❌" not in hudson_text:
                result_parts.append(hudson_text)
            leakcheck_text = format_leakcheck(leakcheck, text)
            if leakcheck_text and "❌" not in leakcheck_text:
                result_parts.append(leakcheck_text)
            psbdmp_text = format_psbdmp(psbdmp, text, "domain")
            if psbdmp_text and "❌" not in psbdmp_text:
                result_parts.append(psbdmp_text)

            if not result_parts:
                await update.message.reply_text("❌ Информация не найдена")
            else:
                await update.message.reply_text("🌍 **Результаты поиска по домену**", parse_mode='Markdown')
                for part in result_parts:
                    if len(part) <= 4096:
                        await update.message.reply_text(part, disable_web_page_preview=True)
                    else:
                        for i in range(0, len(part), 4096):
                            await update.message.reply_text(part[i:i+4096], disable_web_page_preview=True)

        elif action == "phone":
            if not is_phone(text):
                await update.message.reply_text("❌ Некорректный номер. Используйте международный формат, например +79123456789")
                return TYPING_PHONE
            await update.message.reply_text(f"⏳ Анализирую номер {text}...")
            info, err = await get_phone_info(text)
            if err:
                await update.message.reply_text("❌ Информация не найдена")
            else:
                if len(info) <= 4096:
                    await update.message.reply_text(info, parse_mode='Markdown')
                else:
                    for i in range(0, len(info), 4096):
                        await update.message.reply_text(info[i:i+4096], parse_mode='Markdown')

        elif action == "admin_add_bonus":
            if user_id != ADMIN_ID:
                await update.message.reply_text("⛔ У вас нет прав администратора.")
                return await return_to_menu(update)
            if "admin_target_id" not in context.user_data:
                try:
                    target_id = int(text)
                    context.user_data["admin_target_id"] = target_id
                    await update.message.reply_text(f"ID пользователя: {target_id}\nТеперь введите количество бонусов (целое положительное число):")
                    return TYPING_ADMIN_AMOUNT
                except ValueError:
                    await update.message.reply_text("❌ Некорректный ID. Введите число.")
                    return TYPING_ADMIN_USER_ID
            else:
                try:
                    amount = int(text)
                    if amount <= 0:
                        await update.message.reply_text("❌ Количество должно быть положительным числом. Введите ещё раз:")
                        return TYPING_ADMIN_AMOUNT
                    target_id = context.user_data.pop("admin_target_id")
                    await apply_bonus(update, context, target_id, amount)
                except ValueError:
                    await update.message.reply_text("❌ Некорректное число. Введите целое положительное число:")
                    return TYPING_ADMIN_AMOUNT

        else:
            await update.message.reply_text("Неизвестная команда.")

    except Exception as e:
        safe_send_admin(update, context, f"Ошибка в действии {action}: {e}")
        await update.message.reply_text("❌ Внутренняя ошибка. Попробуйте позже.")

    return await return_to_menu(update)

async def return_to_menu(update: Update):
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("🔍 Поиск по нику (соцсети)", callback_data="nick")],
        [InlineKeyboardButton("🆔 Telegram ID по юзернейму", callback_data="tgid")],
        [InlineKeyboardButton("🌐 Информация по IP", callback_data="ip")],
        [InlineKeyboardButton("🐙 GitHub по username", callback_data="github_user")],
        [InlineKeyboardButton("📧 Поиск по email (утечки)", callback_data="email")],
        [InlineKeyboardButton("🌍 Поиск по домену", callback_data="domain")],
        [InlineKeyboardButton("📞 Поиск по номеру телефона", callback_data="phone")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
    ]
    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("🔧 Админ: пополнить запросы", callback_data="admin_add_bonus")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Что хотите найти ещё?", reply_markup=reply_markup)
    return CHOOSING

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.")
    return await return_to_menu(update)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я могу:\n"
        "• Искать профили по нику в соцсетях\n"
        "• Определять ID пользователя Telegram по @username\n"
        "• Показывать информацию по IP\n"
        "• Искать данные пользователя GitHub по username\n"
        "• Искать по email (множество источников)\n"
        "• Искать информацию по домену\n"
        "• Анализировать номер телефона (страна, регион, оператор, часовые пояса, данные с HTMLWeb и PhoneRadar, Avito, соцсети, мессенджеры)\n"
        "• Показать мой профиль и остаток запросов (/profile)\n\n"
        f"Лимит: бесплатные {MAX_REQUESTS_PER_DAY} запроса в день.\n\n"
        "Используй кнопки в меню или просто отправь ник, @username, IP, email, домен или номер."
    )

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    info = get_profile_info(user_id)
    await update.message.reply_text(info, parse_mode='Markdown')

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CHOOSING: [CallbackQueryHandler(button_handler)],
            TYPING_NICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            TYPING_TG_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            TYPING_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            TYPING_GITHUB_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            TYPING_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            TYPING_DOMAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            TYPING_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            TYPING_ADMIN_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            TYPING_ADMIN_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('addbonus', add_bonus))
    application.add_handler(CommandHandler('profile', profile_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input))

    print("Бот запущен и готов к работе (используется .env)")
    application.run_polling()

if __name__ == '__main__':
    main()