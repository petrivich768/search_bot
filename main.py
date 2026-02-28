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

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
LEAKCHECK_KEY = os.getenv('LEAKCHECK_KEY', '')
DADATA_API_KEY = os.getenv('DADATA_API_KEY')
DADATA_SECRET_KEY = os.getenv('DADATA_SECRET_KEY')

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан! Создайте файл .env и укажите токен.")

ADMIN_ID = 8359674526

# Состояния для ConversationHandler
(CHOOSING, TYPING_NICK, TYPING_TG_USERNAME, TYPING_IP,
 TYPING_GITHUB_USERNAME, TYPING_EMAIL, TYPING_DOMAIN, TYPING_PHONE,
 TYPING_MNP, TYPING_TIKTOK_USERNAME,
 TYPING_INN,  # новое состояние
 TYPING_ADMIN_USER_ID, TYPING_ADMIN_AMOUNT) = range(13)

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

def is_inn(text: str):
    # Проверка на 10 или 12 цифр
    return text.isdigit() and len(text) in (10, 12)

# ---------- Вспомогательная функция форматирования в стиле DAMAGE ----------
def format_dict_as_damage(data_dict: dict, title: str = None, indent: int = 0) -> str:
    lines = []
    if title:
        lines.append(f"\n{title}")
    for key, value in data_dict.items():
        if isinstance(value, dict):
            lines.append(f"{'│' * indent}├{key}:")
            lines.append(format_dict_as_damage(value, indent=indent+1))
        elif isinstance(value, list):
            if value:
                lines.append(f"{'│' * indent}├{key}:")
                for item in value[:10]:
                    lines.append(f"{'│' * (indent+1)}├{item}")
                if len(value) > 10:
                    lines.append(f"{'│' * (indent+1)}└... и ещё {len(value)-10}")
            else:
                lines.append(f"{'│' * indent}├{key}: нет данных")
        else:
            lines.append(f"{'│' * indent}├{key}: {value}")
    return "\n".join(lines)

# ---------- Поиск по нику (соцсети) ----------
async def check_social_media(nick: str):
    sites = {
        "Twitter": f"https://twitter.com/{nick}",
        "Instagram": f"https://instagram.com/{nick}",
        "TikTok": f"https://tiktok.com/{nick}",
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
    items = {
        "IP": data.get('query'),
        "Страна": data.get('country'),
        "Регион": data.get('regionName'),
        "Город": data.get('city'),
        "Почтовый индекс": data.get('zip'),
        "Координаты": f"{data.get('lat')}, {data.get('lon')}",
        "Провайдер": data.get('isp'),
        "Организация": data.get('org'),
        "AS": data.get('as')
    }
    items = {k: v for k, v in items.items() if v}
    return format_dict_as_damage(items, title="🌐 Информация по IP")

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
                result['public_gists'] = f'https://gist.github.com/{username}'
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
        async with session.get(ssh_url) as resp:
            if resp.status == 200 and await resp.text():
                result['SSH_keys'] = ssh_url

    if not result:
        return None, "Пользователь не найден"
    return result, None

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
                    result = {
                        "Username": user.get('username', '?'),
                        "Bio": user.get('bio', ''),
                        "Total XP": user.get('totalXp', 0),
                        "From": user.get('courses', [{}])[0].get('fromLanguage', '?') if user.get('courses') else '?'
                    }
                    return format_dict_as_damage(result, title="✅ Duolingo")
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
                        return format_dict_as_damage({"Name": display_name}, title="✅ Gravatar")
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
                        return format_dict_as_damage({"PGP created (UTC)": date}, title="✅ ProtonMail")
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
                        result = {"Username": username, "Profile pic": pic}
                        return format_dict_as_damage(result, title="✅ Instagram")
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
                        result = {"Username": login, "Avatar": avatar}
                        return format_dict_as_damage(result, title="✅ GitHub")
    except Exception:
        pass
    return None

# ---------- Форматирование для Hudson и др. ----------
def format_hudson_standard(data, search_type, query):
    if not isinstance(data, dict) or "error" in data:
        return None
    items = {}
    if "message" in data:
        items["Сообщение"] = data['message']
    if "total_corporate_services" in data or "total_user_services" in data:
        corp = data.get('total_corporate_services', 0)
        user = data.get('total_user_services', 0)
        items["Корп.сервисов"] = corp
        items["Польз.сервисов"] = user
    if "stealers" in data and data["stealers"]:
        stealers_list = []
        for i, stealer in enumerate(data["stealers"][:3], 1):
            date = stealer.get('date_compromised', '?')
            ip = stealer.get('ip', '?')
            os = stealer.get('operating_system', '?')
            stealers_list.append(f"Устройство {i}: {date}, IP {ip}, OS {os}")
            if stealer.get("top_logins"):
                logins = ', '.join(stealer["top_logins"][:3])
                stealers_list.append(f"  Логины: {logins}")
        items["Зараженные устройства"] = stealers_list
    else:
        items["Зараженные устройства"] = "не найдены"
    if items:
        return format_dict_as_damage(items, title="🔍 Hudson Rock")
    return None

def format_hudson_domain(data, query):
    if not isinstance(data, dict) or "error" in data:
        return None
    items = {}
    if "total" in data:
        items["Всего записей"] = data.get('total', 0)
        items["Сотрудников"] = data.get('employees', 0)
        items["Пользователей"] = data.get('users', 0)
    if "data" in data:
        d = data["data"]
        if d.get("employees_urls"):
            items["URL сотрудников"] = [u['url'] for u in d['employees_urls'][:5]]
        if d.get("clients_urls"):
            items["URL клиентов"] = [u['url'] for u in d['clients_urls'][:5]]
    if items:
        return format_dict_as_damage(items, title="🔍 Hudson Rock (домен)")
    return None

def format_leakcheck(data, query):
    if not isinstance(data, dict) or "error" in data:
        return None
    if data.get('success'):
        found = data.get('found', 0)
        if found == 0:
            return None
        items = {"Найдено записей": found}
        if data.get('sources'):
            sources_list = []
            for s in data['sources'][:10]:
                name = s.get('name', '?')
                date = s.get('date', '?')
                sources_list.append(f"{name} ({date})")
            items["Источники"] = sources_list
        return format_dict_as_damage(items, title="✅ LeakCheck")
    return None

def format_proxynova(data, query):
    if not isinstance(data, dict) or "error" in data:
        return None
    proxies = []
    if 'lines' in data:
        proxies = data['lines']
    elif 'proxies' in data:
        proxies = data['proxies']
    elif 'results' in data:
        proxies = data['results']
    if proxies:
        items = {"Найдено записей": len(proxies), "Примеры": proxies[:10]}
        return format_dict_as_damage(items, title="✅ ProxyNova")
    return None

def format_psbdmp(data, query, search_type):
    if not isinstance(data, list) or not data:
        return None
    items = {"Найдено паст": len(data)}
    pastes = []
    for p in data[:10]:
        paste_id = p.get('id', '?')
        tags = p.get('tags', '?')
        pastes.append(f"ID: {paste_id} | Теги: {tags}")
    items["Пасты"] = pastes
    return format_dict_as_damage(items, title="✅ PSBDmp")

# ---------- УЛУЧШЕННАЯ ФУНКЦИЯ ПОИСКА ПО НОМЕРУ ----------
async def get_phone_info(phone: str):
    clean_phone = re.sub(r'[^\d+]', '', phone)
    if not clean_phone:
        return None, "❌ Некорректный номер"

    async with aiohttp.ClientSession() as session:
        htmlweb_data = await _htmlweb_number_scan(session, clean_phone)
        phoneradar_rating, phoneradar_link = await _phoneradar_rating(clean_phone)

    if not htmlweb_data or htmlweb_data.get("error"):
        return None, "❌ Информация не найдена"

    items = {}

    # Основные данные
    if htmlweb_data.get('country'):
        items["Страна"] = htmlweb_data['country']
    if htmlweb_data.get('country_code'):
        items["Код страны"] = htmlweb_data['country_code']
    if htmlweb_data.get('city'):
        items["Город"] = htmlweb_data['city']
    if htmlweb_data.get('postal_code'):
        items["Почтовый индекс"] = htmlweb_data['postal_code']
    if htmlweb_data.get('currency_code'):
        items["Код валюты"] = htmlweb_data['currency_code']
    if htmlweb_data.get('operator'):
        oper = htmlweb_data['operator']
        oper_str = oper.get('brand', '')
        if oper.get('name'):
            oper_str += f" ({oper['name']})"
        if oper.get('url'):
            oper_str += f" - {oper['url']}"
        items["Оператор"] = oper_str
    if htmlweb_data.get('region'):
        items["Регион"] = htmlweb_data['region']
    if htmlweb_data.get('district'):
        items["Округ"] = htmlweb_data['district']
    if htmlweb_data.get('latitude') and htmlweb_data.get('longitude'):
        items["Координаты"] = f"{htmlweb_data['latitude']}, {htmlweb_data['longitude']}"
        items["Карта Google"] = f"https://www.google.com/maps/place/{htmlweb_data['latitude']}+{htmlweb_data['longitude']}"

    # Оценка с phoneradar.ru
    if phoneradar_rating and phoneradar_rating != "Информация отсутствует":
        items["Оценка номера"] = f"{phoneradar_rating} ({phoneradar_link})"

    return format_dict_as_damage(items, title=f"📞 Результаты по номеру {phone}"), None

async def _htmlweb_number_scan(session, phone: str):
    try:
        url = f"https://htmlweb.ru/geo/api.php?json&telcod={phone}"
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                try:
                    data = await resp.json()
                except json.JSONDecodeError:
                    return None
                if data.get('error'):
                    return None
                result = {}
                if 'country' in data:
                    result['country'] = data['country'].get('name', '')
                    result['country_code'] = data['country'].get('iso', '')
                    result['currency_code'] = data['country'].get('iso', '')
                if '0' in data:
                    result['operator'] = {
                        'brand': data['0'].get('oper_brand', ''),
                        'name': data['0'].get('oper', ''),
                        'url': data['0'].get('url', '')
                    }
                    result['city'] = data['0'].get('name', '')
                    result['postal_code'] = data['0'].get('post', '')
                    result['latitude'] = data['0'].get('latitude', '')
                    result['longitude'] = data['0'].get('longitude', '')
                if 'region' in data:
                    result['region'] = data['region'].get('name', '')
                    if 'okrug' in data['region']:
                        result['district'] = data['region']['okrug']
                if 'capital' in data:
                    result['capital'] = data['capital'].get('name', '')
                return result
    except Exception:
        return None

async def _phoneradar_rating(phone: str):
    clean_phone = re.sub(r'[^0-9]', '', phone)
    url = f"https://phoneradar.ru/phone/{clean_phone}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    target_block = soup.find('a', href=f"/phone/{clean_phone[1:]}")
                    if target_block:
                        card_body = target_block.find_parent('div', class_='card-body')
                        if card_body:
                            comment = card_body.find('p').text.strip()
                            name = card_body.find('p').find_next().find_next().text
                            return f"{comment} / {name}", url
    except Exception:
        pass
    return "Информация отсутствует", url

# ---------- ПОИСК MNP ----------
async def get_mnp_info(phone: str):
    clean_phone = re.sub(r'[^\d+]', '', phone)
    if not clean_phone:
        return None, "❌ Некорректный номер"
    url = f"https://htmlweb.ru/json/mnp/phone/{clean_phone}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('error'):
                        return None, "❌ Данные не найдены"
                    items = {}
                    if 'city' in data:
                        items["Город регистрации"] = data['city']
                    if 'region' in data:
                        region = data['region']
                        items["Регион"] = region.get('name', '')
                        if 'okrug' in region:
                            items["Округ"] = region['okrug']
                        if 'autocod' in region:
                            items["Авто-коды"] = region['autocod']
                    if 'oper' in data:
                        oper = data['oper']
                        oper_str = oper.get('brand', '')
                        if oper.get('name'):
                            oper_str += f" ({oper['name']})"
                        if oper.get('url'):
                            oper_str += f" - {oper['url']}"
                        items["Оператор"] = oper_str
                    return format_dict_as_damage(items, title=f"📡 MNP для номера {phone}"), None
                else:
                    return None, "❌ Ошибка API"
    except Exception as e:
        return None, f"❌ Ошибка: {e}"

# ---------- ПОИСК ПО TIKTOK ----------
async def get_tiktok_info(username: str):
    clean_username = username.lstrip('@')
    url = f"https://www.tiktok.com/@{clean_username}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
                soup = BeautifulSoup(html, 'html.parser')
                script_tag = soup.find('script', attrs={'type': 'application/json', 'crossorigin': 'anonymous'})
                if not script_tag:
                    return None
                data = json.loads(script_tag.string)
                user_info = data['props']['pageProps']['userInfo']['user']
                stats = data['props']['pageProps']['userInfo']['stats']
                items = {
                    "UserID": user_info['id'],
                    "Username": user_info['uniqueId'],
                    "Nickname": user_info['nickname'],
                    "Bio": user_info.get('signature', ''),
                    "Profile image": user_info['avatarLarger'],
                    "Following": stats['followingCount'],
                    "Followers": stats['followerCount'],
                    "Likes": stats['heart'],
                    "Videos": stats['videoCount'],
                    "Verified": "Да" if user_info.get('verified') else "Нет"
                }
                return format_dict_as_damage(items, title="🎵 TikTok профиль")
    except Exception:
        return None

# ---------- НОВАЯ ФУНКЦИЯ: ПОИСК ПО ИНН (DaData) ----------
async def get_inn_info(inn: str):
    """Получает информацию об организации или ИП по ИНН через DaData API"""
    if not DADATA_API_KEY or not DADATA_SECRET_KEY:
        return None, "❌ API-ключи DaData не настроены. Добавьте их в файл .env"

    url = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
    headers = {
        "Authorization": f"Token {DADATA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    data = {"query": inn}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if not result.get("suggestions"):
                        return None, "❌ Организация с таким ИНН не найдена"

                    # Берём первое предложение (обычно оно одно)
                    suggestion = result["suggestions"][0]["data"]

                    # Формируем словарь с данными
                    items = {}

                    # Наименование
                    if suggestion.get("name", {}).get("short_with_opf"):
                        items["Наименование"] = suggestion["name"]["short_with_opf"]
                    elif suggestion.get("name", {}).get("full_with_opf"):
                        items["Наименование"] = suggestion["name"]["full_with_opf"]

                    # ИНН/КПП
                    if suggestion.get("inn"):
                        items["ИНН"] = suggestion["inn"]
                    if suggestion.get("kpp"):
                        items["КПП"] = suggestion["kpp"]

                    # ОГРН
                    if suggestion.get("ogrn"):
                        items["ОГРН"] = suggestion["ogrn"]
                    if suggestion.get("ogrn_date"):
                        items["Дата ОГРН"] = suggestion["ogrn_date"]

                    # Статус
                    if suggestion.get("state"):
                        state = suggestion["state"]
                        items["Статус"] = state.get("status", "Н/Д")
                        if state.get("liquidation_date"):
                            items["Дата ликвидации"] = state["liquidation_date"]

                    # Адрес
                    if suggestion.get("address", {}).get("unrestricted_value"):
                        items["Адрес"] = suggestion["address"]["unrestricted_value"]

                    # Координаты
                    if suggestion.get("address", {}).get("data", {}).get("geo_lat"):
                        lat = suggestion["address"]["data"]["geo_lat"]
                        lon = suggestion["address"]["data"]["geo_lon"]
                        items["Координаты"] = f"{lat}, {lon}"
                        items["Карта"] = f"https://yandex.ru/maps/?ll={lon},{lat}&z=16"

                    # ОКВЭД
                    if suggestion.get("okved"):
                        items["Основной ОКВЭД"] = suggestion["okved"]

                    # Руководитель
                    if suggestion.get("management", {}).get("name"):
                        items["Руководитель"] = suggestion["management"]["name"]

                    # Количество филиалов
                    if suggestion.get("branch_count") is not None:
                        items["Филиалов"] = suggestion["branch_count"]

                    # Тип (ЮЛ или ИП)
                    if suggestion.get("type") == "LEGAL":
                        items["Тип"] = "Юридическое лицо"
                    elif suggestion.get("type") == "INDIVIDUAL":
                        items["Тип"] = "Индивидуальный предприниматель"

                    return format_dict_as_damage(items, title=f"📋 Данные по ИНН {inn}"), None
                else:
                    return None, f"❌ Ошибка DaData API: {resp.status}"
    except asyncio.TimeoutError:
        return None, "❌ Таймаут при запросе к DaData"
    except Exception as e:
        return None, f"❌ Ошибка: {e}"

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
        [InlineKeyboardButton("🔄 Поиск MNP", callback_data="mnp")],
        [InlineKeyboardButton("🎵 TikTok по username", callback_data="tiktok")],
        [InlineKeyboardButton("🔎 Поиск по ИНН", callback_data="inn")],  # новая кнопка
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
    elif action == "mnp":
        await query.edit_message_text("Введите номер телефона для MNP-поиска (например, +79123456789):")
        return TYPING_MNP
    elif action == "tiktok":
        await query.edit_message_text("Введите username TikTok (можно с @ или без):")
        return TYPING_TIKTOK_USERNAME
    elif action == "inn":
        await query.edit_message_text("Введите ИНН (10 или 12 цифр):")
        return TYPING_INN
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
                items = {name: url for name, url in found}
                result = format_dict_as_damage(items, title=f"🔍 Найдены профили для '{text}'")
                if len(result) <= 4096:
                    await update.message.reply_text(result)
                else:
                    for i in range(0, len(result), 4096):
                        await update.message.reply_text(result[i:i+4096])
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
            result, err = await github_find_info_by_username(text)
            if err or not result:
                await update.message.reply_text("❌ Информация не найдена")
            else:
                info = format_dict_as_damage(result, title=f"🐙 GitHub: {text}")
                await update.message.reply_text(info)

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
            for res in [hudson, leakcheck, proxynova, psbdmp, duolingo, gravatar, imgur, mailru, protonmail, bitmoji, instagram, twitter, github]:
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
            for res in [hudson, leakcheck, psbdmp]:
                if res and isinstance(res, str) and not res.startswith("❌"):
                    result_parts.append(res)

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

        elif action == "mnp":
            if not is_phone(text):
                await update.message.reply_text("❌ Некорректный номер. Используйте международный формат, например +79123456789")
                return TYPING_MNP
            await update.message.reply_text(f"⏳ Ищу MNP для номера {text}...")
            info, err = await get_mnp_info(text)
            if err:
                await update.message.reply_text(err)
            else:
                if len(info) <= 4096:
                    await update.message.reply_text(info, parse_mode='Markdown')
                else:
                    for i in range(0, len(info), 4096):
                        await update.message.reply_text(info[i:i+4096], parse_mode='Markdown')

        elif action == "tiktok":
            await update.message.reply_text(f"⏳ Ищу информацию о TikTok пользователе @{text.lstrip('@')}...")
            result = await get_tiktok_info(text)
            if not result:
                await update.message.reply_text("❌ Информация не найдена")
            else:
                if len(result) <= 4096:
                    await update.message.reply_text(result, parse_mode='Markdown', disable_web_page_preview=True)
                else:
                    for i in range(0, len(result), 4096):
                        await update.message.reply_text(result[i:i+4096], parse_mode='Markdown', disable_web_page_preview=True)

        elif action == "inn":
            if not is_inn(text):
                await update.message.reply_text("❌ ИНН должен содержать 10 или 12 цифр. Попробуйте снова.")
                return TYPING_INN
            await update.message.reply_text(f"⏳ Ищу информацию по ИНН {text}...")
            info, err = await get_inn_info(text)
            if err:
                await update.message.reply_text(err)
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
        [InlineKeyboardButton("🔍 Поиск по нику", callback_data="nick")],
        [InlineKeyboardButton("🆔 Telegram ID по username", callback_data="tgid")],
        [InlineKeyboardButton("🌐 Информация по IP", callback_data="ip")],
        [InlineKeyboardButton("🐙 GitHub по username", callback_data="github_user")],
        [InlineKeyboardButton("📧 Поиск по email", callback_data="email")],
        [InlineKeyboardButton("🌍 Поиск по домену", callback_data="domain")],
        [InlineKeyboardButton("📞 Поиск по номеру телефона", callback_data="phone")],
        [InlineKeyboardButton("🔄 Поиск MNP", callback_data="mnp")],
        [InlineKeyboardButton("🎵 TikTok по username", callback_data="tiktok")],
        [InlineKeyboardButton("🔎 Поиск по ИНН", callback_data="inn")],
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
        "• Анализировать номер телефона (подробно: страна, регион, оператор, координаты, карта, оценка с phoneradar.ru)\n"
        "• Искать MNP (переносимость номера) – регион, оператор, авто-коды\n"
        "• Искать информацию о пользователе TikTok\n"
        "• Искать информацию об организации или ИП по ИНН (через DaData)\n"
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
            TYPING_MNP: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            TYPING_TIKTOK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            TYPING_INN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
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

    print("Бот запущен и готов к работе (с поиском по ИНН)")
    application.run_polling()

if __name__ == '__main__':
    main()