import os
import asyncio
import logging
import json
import re
import random
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set, Tuple
import aiohttp
from aiohttp import web
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

# --- КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---
PORT = int(os.environ.get("PORT", 8000))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ваш_канал")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен!")
if not RENDER_EXTERNAL_URL:
    raise ValueError("RENDER_EXTERNAL_URL не установлен!")

# --- НАСТРОЙКИ ЛОГГИРОВАНИЯ ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- ФАЙЛЫ ДЛЯ ХРАНЕНИЯ ДАННЫХ ---
SENT_NUMBERS_FILE = "sent_numbers.json"
SENT_NFT_FILE = "sent_nft.json"
SENT_UPDATES_FILE = "sent_updates.json"
STATS_FILE = "stats.json"

class FragmentMonitor:
    def __init__(self):
        self.token = BOT_TOKEN
        self.channel_id = CHANNEL_ID
        self.app = None
        
        # URL для парсинга
        self.fragment_url = "https://fragment.com"
        self.numbers_url = f"{self.fragment_url}/numbers"
        self.nft_url = f"{self.fragment_url}/nft"
        self.updates_url = f"{self.fragment_url}"
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Cache-Control': 'no-cache'
        }
        
        # Кэш для курсов валют
        self.rates = {
            'usd_rub': 90.0,
            'usd_eur': 0.92,
            'ton_usd': 2.5,
            'eth_usd': 3000,
            'btc_usd': 65000
        }
        
        # Загружаем данные
        self.sent_numbers = self.load_data(SENT_NUMBERS_FILE, set)
        self.sent_nft = self.load_data(SENT_NFT_FILE, set)
        self.sent_updates = self.load_data(SENT_UPDATES_FILE, set)
        self.stats = self.load_data(STATS_FILE, dict, {
            'total_numbers': 0,
            'total_nft': 0,
            'total_updates': 0,
            'last_check': None,
            'avg_price_numbers': 0,
            'avg_price_nft': 0,
            'min_price': float('inf'),
            'max_price': 0
        })
        
        # Эмодзи для оформления
        self.emoji = {
            'new': '🆕', 'hot': '🔥', 'premium': '💎', 'crown': '👑',
            'rocket': '🚀', 'chart': '📊', 'up': '📈', 'down': '📉',
            'phone': '📞', 'money': '💰', 'time': '🕐', 'link': '🔗',
            'star': '⭐', 'diamond': '💎', 'thunder': '⚡️', 'alert': '⚠️',
            'chart_up': '📈', 'chart_down': '📉', 'target': '🎯',
            'nft': '🖼️', 'update': '🔄', 'fire': '🔥', 'flash': '⚡',
            'graph': '📈', 'wallet': '👛', 'gift': '🎁', 'auction': '🏆'
        }

    def load_data(self, filename: str, data_type: type, default=None):
        """Загрузка данных из JSON"""
        try:
            if os.path.exists(filename):
                with open(filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if data_type == set:
                        return set(data)
                    return data
        except Exception as e:
            logger.error(f"Ошибка загрузки {filename}: {e}")
        
        return default if default is not None else (set() if data_type == set else {})

    def save_data(self, filename: str, data):
        """Сохранение данных в JSON"""
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                if isinstance(data, set):
                    json.dump(list(data), f, ensure_ascii=False, indent=2)
                else:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения {filename}: {e}")

    async def update_rates(self):
        """Обновление курсов валют"""
        try:
            async with aiohttp.ClientSession() as session:
                # Фиатные курсы
                async with session.get('https://api.exchangerate-api.com/v4/latest/USD') as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.rates['usd_rub'] = data['rates']['RUB']
                        self.rates['usd_eur'] = data['rates']['EUR']
                
                # Крипто курсы
                async with session.get('https://api.coingecko.com/api/v3/simple/price?ids=the-open-network,ethereum,bitcoin&vs_currencies=usd') as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.rates['ton_usd'] = data['the-open-network']['usd']
                        self.rates['eth_usd'] = data['ethereum']['usd']
                        self.rates['btc_usd'] = data['bitcoin']['usd']
            
            logger.info(f"✅ Курсы: TON=${self.rates['ton_usd']:.3f}, ETH=${self.rates['eth_usd']:,.0f}")
        except Exception as e:
            logger.error(f"❌ Ошибка обновления курсов: {e}")

    def format_price(self, price_usd: float, currency: str = 'USD') -> str:
        """Красивое форматирование цены"""
        rub = price_usd * self.rates['usd_rub']
        eur = price_usd * self.rates['usd_eur']
        ton = price_usd / self.rates['ton_usd']
        eth = price_usd / self.rates['eth_usd']
        btc = price_usd / self.rates['btc_usd']
        
        def format_value(val: float) -> str:
            if val < 0.001:
                return f"{val:.6f}"
            elif val < 0.01:
                return f"{val:.5f}"
            elif val < 0.1:
                return f"{val:.4f}"
            elif val < 1:
                return f"{val:.3f}"
            elif val < 1000:
                return f"{val:,.2f}"
            elif val < 1_000_000:
                return f"{val/1000:.1f}K"
            else:
                return f"{val/1_000_000:.1f}M"
        
        lines = [
            f"┌─ <b>💰 Цена</b>",
            f"│ • ${format_value(price_usd)} USD",
            f"│ • ₽{format_value(rub)} RUB",
            f"│ • €{format_value(eur)} EUR",
            f"│ • {format_value(ton)} TON",
            f"│ • Ξ{format_value(eth)} ETH",
            f"│ • ₿{format_value(btc)} BTC",
            "└──────────"
        ]
        
        return "\n".join(lines)

    def format_ton_price(self, price_ton: float) -> str:
        """Форматирование цены в TON"""
        usd = price_ton * self.rates['ton_usd']
        return self.format_price(usd)

    # ========== 1. НОМЕРА +888 ==========

    async def parse_numbers(self) -> List[Dict]:
        """Парсинг номеров +888"""
        numbers = []
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.numbers_url, headers=self.headers) as response:
                    if response.status != 200:
                        return numbers
                    
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    items = soup.find_all('a', class_=re.compile('tm-row|tm-cell'))
                    
                    for item in items[:20]:
                        try:
                            number_elem = item.find('div', class_='tm-number')
                            if not number_elem:
                                continue
                            
                            number_text = number_elem.get_text(strip=True)
                            
                            if '+888' not in number_text:
                                continue
                            
                            price_elem = item.find('div', class_='tm-price')
                            if not price_elem:
                                continue
                            
                            price_text = price_elem.get_text(strip=True)
                            price_match = re.search(r'\$([0-9,.KMB]+)', price_text)
                            if not price_match:
                                continue
                            
                            price_str = price_match.group(1)
                            
                            # Конвертация цены
                            multiplier = 1
                            if 'K' in price_str:
                                multiplier = 1000
                                price_str = price_str.replace('K', '')
                            elif 'M' in price_str:
                                multiplier = 1_000_000
                                price_str = price_str.replace('M', '')
                            
                            price_usd = float(price_str.replace(',', '')) * multiplier
                            
                            # TON цена
                            ton_elem = item.find('span', class_='tm-ton-price')
                            ton_price = None
                            if ton_elem:
                                ton_text = ton_elem.get_text(strip=True)
                                ton_match = re.search(r'([0-9,.]+)', ton_text)
                                if ton_match:
                                    ton_price = float(ton_match.group(1).replace(',', ''))
                            
                            link = item.get('href', '')
                            if link and not link.startswith('http'):
                                link = f"{self.fragment_url}{link}"
                            
                            item_id = f"num_{number_text}_{price_usd:.0f}"
                            
                            if item_id not in self.sent_numbers:
                                numbers.append({
                                    'id': item_id,
                                    'type': 'number',
                                    'title': f"Коллекционный номер {number_text}",
                                    'number': number_text,
                                    'price_usd': price_usd,
                                    'ton_price': ton_price,
                                    'url': link or self.numbers_url,
                                    'found_at': datetime.now().isoformat()
                                })
                                logger.info(f"✅ Номер: {number_text} - ${price_usd:,.0f}")
                        
                        except Exception:
                            continue
                    
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга номеров: {e}")
        
        return numbers

    # ========== 2. NFT ==========

    async def parse_nft(self) -> List[Dict]:
        """Парсинг NFT на Fragment"""
        nft_items = []
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.nft_url, headers=self.headers) as response:
                    if response.status != 200:
                        return nft_items
                    
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    items = soup.find_all('a', class_=re.compile('tm-row|tm-cell'))
                    
                    for item in items[:20]:
                        try:
                            # Название NFT
                            name_elem = item.find('div', class_='tm-name') or item.find('span', class_='tm-name')
                            if not name_elem:
                                continue
                            
                            name = name_elem.get_text(strip=True)
                            
                            # Описание
                            desc_elem = item.find('div', class_='tm-description')
                            description = desc_elem.get_text(strip=True)[:100] if desc_elem else "Коллекционный предмет"
                            
                            # Цена
                            price_elem = item.find('div', class_='tm-price')
                            if not price_elem:
                                continue
                            
                            price_text = price_elem.get_text(strip=True)
                            price_match = re.search(r'\$([0-9,.KMB]+)', price_text)
                            if not price_match:
                                continue
                            
                            price_str = price_match.group(1)
                            
                            multiplier = 1
                            if 'K' in price_str:
                                multiplier = 1000
                                price_str = price_str.replace('K', '')
                            elif 'M' in price_str:
                                multiplier = 1_000_000
                                price_str = price_str.replace('M', '')
                            
                            price_usd = float(price_str.replace(',', '')) * multiplier
                            
                            # TON цена
                            ton_elem = item.find('span', class_='tm-ton-price')
                            ton_price = None
                            if ton_elem:
                                ton_text = ton_elem.get_text(strip=True)
                                ton_match = re.search(r'([0-9,.]+)', ton_text)
                                if ton_match:
                                    ton_price = float(ton_match.group(1).replace(',', ''))
                            
                            # Изображение
                            img_elem = item.find('img')
                            img_url = img_elem.get('src') if img_elem else None
                            if img_url and not img_url.startswith('http'):
                                img_url = f"{self.fragment_url}{img_url}"
                            
                            link = item.get('href', '')
                            if link and not link.startswith('http'):
                                link = f"{self.fragment_url}{link}"
                            
                            item_id = f"nft_{name}_{price_usd:.0f}"
                            
                            if item_id not in self.sent_nft:
                                nft_items.append({
                                    'id': item_id,
                                    'type': 'nft',
                                    'title': name,
                                    'description': description,
                                    'price_usd': price_usd,
                                    'ton_price': ton_price,
                                    'image_url': img_url,
                                    'url': link or self.nft_url,
                                    'found_at': datetime.now().isoformat()
                                })
                                logger.info(f"✅ NFT: {name} - ${price_usd:,.0f}")
                        
                        except Exception:
                            continue
                    
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга NFT: {e}")
        
        return nft_items

    # ========== 3. ОБНОВЛЕНИЯ САЙТА ==========

    async def parse_updates(self) -> List[Dict]:
        """Парсинг обновлений сайта"""
        updates = []
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.updates_url, headers=self.headers) as response:
                    if response.status != 200:
                        return updates
                    
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    # Ищем новости и обновления
                    news_items = soup.find_all(['div', 'article'], class_=re.compile('news|update|announce|post'))
                    
                    if not news_items:
                        news_items = soup.find_all(['div', 'a'], class_=re.compile('tm-news|tm-update'))
                    
                    for item in news_items[:10]:
                        try:
                            title_elem = item.find(['h2', 'h3', 'h4', 'a', 'div'], class_=re.compile('title|name|head'))
                            if not title_elem:
                                title_elem = item
                            
                            title = title_elem.get_text(strip=True)[:100]
                            
                            if len(title) < 10:  # Слишком короткий заголовок
                                continue
                            
                            # Описание
                            desc_elem = item.find(['p', 'div'], class_=re.compile('desc|text|content'))
                            description = desc_elem.get_text(strip=True)[:150] if desc_elem else ""
                            
                            # Дата
                            date_elem = item.find(['time', 'span'], class_=re.compile('date|time'))
                            date_str = date_elem.get_text(strip=True) if date_elem else "только что"
                            
                            # Ссылка
                            link_elem = item.find('a')
                            link = link_elem.get('href') if link_elem else ""
                            if link and not link.startswith('http'):
                                link = f"{self.fragment_url}{link}"
                            
                            item_id = f"upd_{title[:30]}_{datetime.now().timestamp()}"
                            
                            if item_id not in self.sent_updates:
                                updates.append({
                                    'id': item_id,
                                    'type': 'update',
                                    'title': title,
                                    'description': description,
                                    'date': date_str,
                                    'url': link or self.updates_url,
                                    'found_at': datetime.now().isoformat()
                                })
                                logger.info(f"✅ Обновление: {title[:50]}...")
                        
                        except Exception:
                            continue
                    
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга обновлений: {e}")
        
        return updates

    # ========== ФОРМАТИРОВАНИЕ ПОСТОВ ==========

    def get_status(self, price_usd: float) -> Tuple[str, str]:
        """Определение статуса по цене"""
        if price_usd > 50000:
            return "👑 LEGENDARY", "🔥"
        elif price_usd > 10000:
            return "💎 ELITE", "⭐"
        elif price_usd > 5000:
            return "🌟 PREMIUM", "💫"
        elif price_usd > 1000:
            return "✨ STANDARD", "📌"
        else:
            return "🆕 ECONOMY", "💭"

    def create_number_post(self, item: Dict) -> str:
        """Пост для номера"""
        status, status_emoji = self.get_status(item['price_usd'])
        price_block = self.format_price(item['price_usd'])
        found_time = datetime.fromisoformat(item['found_at']).strftime("%d.%m.%Y · %H:%M")
        
        # Очищаем номер от пробелов для хэштега
        number_clean = re.sub(r'\s+', '', item['number'])
        
        post = f"""
{self.emoji['phone']} <b>КОЛЛЕКЦИОННЫЙ НОМЕР TELEGRAM</b> {self.emoji['phone']}

━━━━━━━━━━━━━━━━━━━━━
{status_emoji} <b>{status}</b>

📱 <b>Номер:</b>
<code>{item['number']}</code>

{price_block}

📊 <b>Информация:</b>
│ • Платформа: Fragment.com
│ • Тип: Цифровой актив TON
│ • Статус: В продаже
│ • Доступен для покупки

🔍 <b>Особенности:</b>
│ • Не привязан к SIM-карте
│ • Вход в Telegram без SIM
│ • Можно продать/передать
│ • Полная совместимость с TON

🕐 Обнаружено: {found_time}
━━━━━━━━━━━━━━━━━━━━━

#Telegram #Number #{number_clean[:7]} #TON #Fragment #Коллекционный
        """
        
        return post.strip()

    def create_nft_post(self, item: Dict) -> str:
        """Пост для NFT"""
        status, status_emoji = self.get_status(item['price_usd'])
        price_block = self.format_price(item['price_usd'])
        found_time = datetime.fromisoformat(item['found_at']).strftime("%d.%m.%Y · %H:%M")
        
        # Очищаем название для хэштега
        name_clean = re.sub(r'[^\w]', '', item['title'])[:20]
        
        post = f"""
{self.emoji['nft']} <b>NFT КОЛЛЕКЦИЯ НА FRAGMENT</b> {self.emoji['nft']}

━━━━━━━━━━━━━━━━━━━━━
{status_emoji} <b>{status}</b>

🖼️ <b>Название:</b>
{item['title']}

📝 <b>Описание:</b>
{item['description']}

{price_block}

📊 <b>Характеристики:</b>
│ • Платформа: Fragment.com
│ • Тип: NFT (TON)
│ • Статус: В продаже
│ • Торговая площадка: Fragment

🔍 <b>Детали:</b>
│ • Уникальный цифровой предмет
│ • Хранится в блокчейне TON
│ • Можно перепродать
│ • Редкость: {status}

🕐 Обнаружено: {found_time}
━━━━━━━━━━━━━━━━━━━━━

#NFT #TON #Fragment #{name_clean} #Коллекционирование
        """
        
        return post.strip()

    def create_update_post(self, item: Dict) -> str:
        """Пост для обновления"""
        found_time = datetime.fromisoformat(item['found_at']).strftime("%d.%m.%Y · %H:%M")
        
        post = f"""
{self.emoji['update']} <b>ОБНОВЛЕНИЕ НА FRAGMENT</b> {self.emoji['update']}

━━━━━━━━━━━━━━━━━━━━━

📢 <b>{item['title']}</b>

📝 <b>Описание:</b>
{item['description'] if item['description'] else 'Новое обновление на платформе'}

📅 <b>Дата:</b>
{item['date']}

🔗 <b>Подробнее:</b>
Переходите по ссылке ниже

🕐 Обнаружено: {found_time}
━━━━━━━━━━━━━━━━━━━━━

#Fragment #Update #Новости #TON #Обновление
        """
        
        return post.strip()

    async def send_to_channel(self, item: Dict):
        """Отправка поста в канал"""
        
        # Выбираем тип поста
        if item['type'] == 'number':
            post_text = self.create_number_post(item)
            emoji = self.emoji['phone']
            file_to_save = SENT_NUMBERS_FILE
            data_set = self.sent_numbers
            self.stats['total_numbers'] += 1
            self.stats['avg_price_numbers'] = (self.stats['avg_price_numbers'] * (self.stats['total_numbers'] - 1) + item['price_usd']) / self.stats['total_numbers']
            
        elif item['type'] == 'nft':
            post_text = self.create_nft_post(item)
            emoji = self.emoji['nft']
            file_to_save = SENT_NFT_FILE
            data_set = self.sent_nft
            self.stats['total_nft'] += 1
            self.stats['avg_price_nft'] = (self.stats['avg_price_nft'] * (self.stats['total_nft'] - 1) + item['price_usd']) / self.stats['total_nft']
            
        else:  # update
            post_text = self.create_update_post(item)
            emoji = self.emoji['update']
            file_to_save = SENT_UPDATES_FILE
            data_set = self.sent_updates
            self.stats['total_updates'] += 1
        
        # Обновляем общую статистику
        if 'price_usd' in item:
            self.stats['min_price'] = min(self.stats['min_price'], item['price_usd'])
            self.stats['max_price'] = max(self.stats['max_price'], item['price_usd'])
        
        self.stats['last_check'] = datetime.now().isoformat()
        
        # Кнопки
        buttons = []
        
        # Основная кнопка
        if item['url']:
            buttons.append([InlineKeyboardButton(f"{self.emoji['link']} Перейти к лоту", url=item['url'])])
        
        # Дополнительные кнопки
        second_row = []
        if item['type'] in ['number', 'nft']:
            second_row.append(InlineKeyboardButton(f"{self.emoji['chart']} График", callback_data=f"chart_{item['id']}"))
        second_row.append(InlineKeyboardButton(f"{self.emoji['money']} Конвертер", callback_data=f"convert_{item['id']}"))
        buttons.append(second_row)
        
        # Кнопка подписки
        buttons.append([InlineKeyboardButton(f"{self.emoji['target']} Подписаться", url=f"https://t.me/{self.channel_id.replace('@', '')}")])
        
        keyboard = InlineKeyboardMarkup(buttons)
        
        try:
            # Отправляем с изображением если есть
            if item.get('image_url'):
                await self.app.bot.send_photo(
                    chat_id=self.channel_id,
                    photo=item['image_url'],
                    caption=post_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard
                )
            else:
                await self.app.bot.send_message(
                    chat_id=self.channel_id,
                    text=post_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
            
            # Сохраняем в историю
            data_set.add(item['id'])
            self.save_data(file_to_save, data_set)
            self.save_data(STATS_FILE, self.stats)
            
            logger.info(f"✅ Отправлен {item['type']}: {item.get('title', item.get('number', 'Unknown'))}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки: {e}")
            return False

    async def monitor_loop(self):
        """Основной цикл мониторинга"""
        logger.info("🔍 Запуск мониторинга Fragment.com...")
        
        while True:
            try:
                # Обновляем курсы
                await self.update_rates()
                
                # Собираем все новые данные параллельно
                numbers_task = self.parse_numbers()
                nft_task = self.parse_nft()
                updates_task = self.parse_updates()
                
                numbers, nft_items, updates = await asyncio.gather(
                    numbers_task, nft_task, updates_task
                )
                
                all_items = []
                all_items.extend(numbers)
                all_items.extend(nft_items)
                all_items.extend(updates)
                
                if all_items:
                    logger.info(f"📢 Найдено всего: {len(all_items)} (номера: {len(numbers)}, NFT: {len(nft_items)}, обновления: {len(updates)})")
                    
                    # Сортируем: сначала номера и NFT (по цене), потом обновления
                    items_with_price = [i for i in all_items if 'price_usd' in i]
                    items_with_price.sort(key=lambda x: x['price_usd'], reverse=True)
                    
                    updates_only = [i for i in all_items if i['type'] == 'update']
                    
                    # Отправляем сначала ценные предметы
                    for item in items_with_price:
                        await self.send_to_channel(item)
                        await asyncio.sleep(random.randint(180, 300))  # 3-5 минут
                    
                    # Потом обновления
                    for item in updates_only:
                        await self.send_to_channel(item)
                        await asyncio.sleep(random.randint(120, 240))  # 2-4 минуты
                
                else:
                    logger.info("😴 Новых предметов не найдено")
                
                # Рандомная задержка (15-30 минут)
                wait_time = random.randint(900, 1800)
                logger.info(f"⏳ Следующая проверка через {wait_time//60} минут")
                await asyncio.sleep(wait_time)
                
            except Exception as e:
                logger.error(f"⚠️ Ошибка в цикле: {e}")
                await asyncio.sleep(300)

    # ========== КОМАНДЫ ==========

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        welcome = f"""
{self.emoji['rocket']} <b>FRAGMENT MONITOR BOT</b> {self.emoji['rocket']}

Отслеживаю на Fragment.com:
📞 Номера +888
🖼️ NFT коллекции
🔄 Обновления сайта

📊 <b>Статистика:</b>
• Номера: {self.stats['total_numbers']}
• NFT: {self.stats['total_nft']}
• Обновления: {self.stats['total_updates']}
• Средняя цена номеров: ${self.stats['avg_price_numbers']:,.0f}
• Средняя цена NFT: ${self.stats['avg_price_nft']:,.0f}

💱 <b>Курсы:</b>
• TON/USD: ${self.rates['ton_usd']:.3f}
• ETH/USD: ${self.rates['eth_usd']:,.0f}
• USD/RUB: {self.rates['usd_rub']:.2f}

📱 <b>Команды:</b>
/latest — последние находки
/stats — полная статистика
/help — помощь
        """
        await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)

    async def latest_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Последние находки"""
        text = f"""
{self.emoji['chart']} <b>ПОСЛЕДНИЕ НАХОДКИ</b>

📞 <b>Номера:</b>
• Всего найдено: {self.stats['total_numbers']}
• Средняя цена: ${self.stats['avg_price_numbers']:,.0f}

🖼️ <b>NFT:</b>
• Всего найдено: {self.stats['total_nft']}
• Средняя цена: ${self.stats['avg_price_nft']:,.0f}

🔄 <b>Обновления:</b>
• Всего: {self.stats['total_updates']}
• Последнее: {self.stats['last_check'][:10] if self.stats['last_check'] else 'нет'}

💡 <i>Новые посты появляются в канале каждые 15-30 минут</i>
        """
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Детальная статистика"""
        stats_text = f"""
{self.emoji['chart']} <b>ДЕТАЛЬНАЯ СТАТИСТИКА</b>

📊 <b>Общая информация:</b>
• Номера +888: {self.stats['total_numbers']}
• NFT: {self.stats['total_nft']}
• Обновления: {self.stats['total_updates']}
• Всего находок: {self.stats['total_numbers'] + self.stats['total_nft'] + self.stats['total_updates']}
• Последняя проверка: {self.stats['last_check'][:16] if self.stats['last_check'] else 'никогда'}

💰 <b>Цены:</b>
• Средняя (номера): ${self.stats['avg_price_numbers']:,.0f}
• Средняя (NFT): ${self.stats['avg_price_nft']:,.0f}
• Минимальная: ${self.stats['min_price']:,.0f}
• Максимальная: ${self.stats['max_price']:,.0f}

💱 <b>Курсы валют:</b>
• TON/USD: ${self.rates['ton_usd']:.3f}
• ETH/USD: ${self.rates['eth_usd']:,.0f}
• BTC/USD: ${self.rates['btc_usd']:,.0f}
• USD/RUB: {self.rates['usd_rub']:.2f}
• USD/EUR: {self.rates['usd_eur']:.2f}

🔥 <b>Статус:</b>
• Бот: Активен
• Мониторинг: 24/7
• Интервал: 15-30 минут
        """
        await update.message.reply_text(stats_text, parse_mode=ParseMode.HTML)

    # ========== ВЕБ-ХУКИ ==========

    async def health_check(self, request):
        """Проверка здоровья"""
        return web.Response(text="OK")

    async def webhook_handler(self, request):
        """Обработчик веб-хуков"""
        try:
            data = await request.json()
            update = Update.de_json(data, self.app.bot)
            await self.app.process_update(update)
            return web.Response(text="OK")
        except Exception as e:
            logger.error(f"Ошибка webhook: {e}")
            return web.Response(status=500)

    async def setup_webhook(self):
        """Настройка веб-хука"""
        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
        await self.app.bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True
        )
        logger.info(f"✅ Веб-хук: {webhook_url}")

    async def run(self):
        """Запуск бота"""
        # Инициализация приложения
        self.app = Application.builder().token(self.token).build()
        
        # Регистрация команд
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("latest", self.latest_command))
        self.app.add_handler(CommandHandler("stats", self.stats_command))
        
        # Обновление курсов
        await self.update_rates()
        
        # Настройка веб-хука
        await self.setup_webhook()
        
        # Запуск мониторинга
        asyncio.create_task(self.monitor_loop())
        
        # Веб-сервер
        web_app = web.Application()
        web_app.router.add_get("/health", self.health_check)
        web_app.router.add_post("/webhook", self.webhook_handler)
        
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        
        logger.info(f"🚀 Бот запущен на порту {PORT}")
        
        # Держим запущенным
        await asyncio.Event().wait()

# --- ЗАПУСК ---
if __name__ == "__main__":
    bot = FragmentMonitor()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
