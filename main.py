"""Telegram-бот: уведомления о появлении объявлений отслеживаемых мерчантов на Bybit P2P."""
import hashlib
import html
import logging
import os
import queue
import tempfile
import threading
import time
from datetime import timedelta
from logging.handlers import RotatingFileHandler

import pandas as pd
import telebot
from sqlalchemy import func
from telebot import types
from telebot.apihelper import ApiTelegramException

from app_config import ADMIN_CONTACT, ADMIN_GROUP_ID, BOT_TOKEN, CHECK_INTERVAL, LOG_PATH
from checker import bybit_check_all, fetch_payment_names
from const import PAYMENT_NAMES_FALLBACK
from database import (SIDE_FILTERS, OfferLog, SentOffer, SessionLocal, TrackedMerchant, UserSettings,
                      cleanup_sent_offers, get_or_create_settings, init_db, load_recent_sent, utcnow)

if not BOT_TOKEN:
    raise SystemExit('BOT_TOKEN не задан: скопируй .env.example в .env и впиши токен от @BotFather.')

# === НАСТРОЙКИ ===
SENT_TTL_DAYS = 7              # сколько помним отправленные офферы
CLEANUP_INTERVAL = 3600        # секунд между чистками sent_offers
API_ALERT_AFTER = 30           # циклов подряд без ответа Bybit (~5 мин) -> алерт админам
REMOVAL_CONFIRM_CYCLES = 2     # сколько циклов объявления нет в стакане, чтобы считать его снятым
MAX_MERCHANT_NAME = 40
EXPORT_ROWS_LIMIT = 50000
REMARK_PREVIEW = 80
RANK_STEPS = (0, 10, 30, 100)  # «место в стакане», 0 = любое
BYBIT_PROFILE_URL = 'https://www.bybit.com/ru-RU/fiat/trade/otc/profile/{mask}/USDT/RUB/item'

os.makedirs(os.path.dirname(os.path.abspath(LOG_PATH)), exist_ok=True)
_handlers = [RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'),
             logging.StreamHandler()]
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO, handlers=_handlers)
logging.getLogger('TeleBot').setLevel(logging.WARNING)
log = logging.getLogger('bot')

bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
user_states = {}
send_queue = queue.Queue()
payment_names = dict(PAYMENT_NAMES_FALLBACK)

# Последний снимок стакана: by_nick {nick_lower: [объявления с полями _side и _rank]}
scan_lock = threading.Lock()
scan_state = {'ts': 0.0, 'by_nick': {}}
# Короткий ключ ника для callback_data (лимит 64 байта) -> отображаемый ник
known_nicks = {}

SIDE_LABELS = {'ALL': 'все', 'BUY': 'только покупка', 'SELL': 'только продажа'}
SIDE_ICONS = {'BUY': '🟢 Покупка', 'SELL': '🔴 Продажа'}


# === УТИЛИТЫ ===
def esc(value) -> str:
    return html.escape(str(value if value is not None else ''))


def fmt_amount(value) -> str:
    """'160000.00' -> '160 000', '500.5' -> '500.50'"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num.is_integer():
        return f'{int(num):,}'.replace(',', ' ')
    return f'{num:,.2f}'.replace(',', ' ')


def fmt_ago(ts_seconds) -> str:
    try:
        delta = max(0, int(time.time() - int(ts_seconds)))
    except (TypeError, ValueError):
        return ''
    if delta < 3600:
        return f'{max(1, delta // 60)} мин назад'
    if delta < 86400:
        return f'{delta // 3600} ч назад'
    return f'{delta // 86400} дн назад'


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def nick_key(nick: str) -> str:
    return hashlib.md5((nick or '').strip().lower().encode()).hexdigest()[:16]


def remember_nick(nick: str) -> str:
    key = nick_key(nick)
    known_nicks[key] = (nick or '').strip()
    return key


def notify_admin(text: str):
    if not ADMIN_GROUP_ID:
        return
    try:
        bot.send_message(ADMIN_GROUP_ID, text)
    except Exception as e:
        log.warning('Не удалось отправить в админ-группу: %s', e)


def compute_offer_hash(offer: dict) -> str:
    raw = f"{offer.get('id')}|{offer.get('price')}|{offer.get('minAmount')}|{offer.get('maxAmount')}"
    return hashlib.md5(raw.encode()).hexdigest()


def compute_ranks(results: dict) -> dict:
    """{side: {offer_id: место в стакане}} — порядок, в котором Bybit отдаёт (и показывает) объявления."""
    ranks = {}
    for trade_type, items in results.items():
        ranks[trade_type] = {offer.get('id'): idx + 1 for idx, offer in enumerate(items or [])}
    return ranks


def update_scan_state(results: dict, ranks: dict = None):
    ranks = ranks or compute_ranks(results)
    by_nick = {}
    for trade_type, items in results.items():
        for offer in items or []:
            nick = (offer.get('nickName') or '').strip()
            if not nick:
                continue
            entry = dict(offer)
            entry['_side'] = trade_type
            entry['_rank'] = ranks.get(trade_type, {}).get(offer.get('id'))
            by_nick.setdefault(nick.lower(), []).append(entry)
    with scan_lock:
        scan_state['by_nick'] = by_nick
        scan_state['ts'] = time.time()


def ensure_user(session, tg_user):
    """Создаёт запись пользователя и настройки, обновляет username."""
    row = session.query(TrackedMerchant).filter_by(user_id=tg_user.id).first()
    if row:
        if row.username != tg_user.username:
            session.query(TrackedMerchant).filter_by(user_id=tg_user.id).update({'username': tg_user.username})
    else:
        session.add(TrackedMerchant(user_id=tg_user.id, username=tg_user.username, merchant_name=''))
    session.commit()
    return get_or_create_settings(session, tg_user.id)


def get_merchants(session, user_id):
    return (session.query(TrackedMerchant)
            .filter(TrackedMerchant.user_id == user_id, TrackedMerchant.merchant_name != '')
            .order_by(func.lower(TrackedMerchant.merchant_name))
            .all())


def find_tracked(session, user_id, nick):
    return (session.query(TrackedMerchant)
            .filter(TrackedMerchant.user_id == user_id,
                    func.lower(TrackedMerchant.merchant_name) == (nick or '').strip().lower())
            .first())


# === КЛАВИАТУРЫ ===
def build_settings_markup(merchants, settings) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    for m in merchants:
        remember_nick(m.merchant_name)
        markup.row(types.InlineKeyboardButton(f'ℹ️ {m.merchant_name}', callback_data=f'info_id_{m.id}'),
                   types.InlineKeyboardButton('❌', callback_data=f'del_{m.id}'))
    markup.add(types.InlineKeyboardButton('➕ Добавить мерчанта', callback_data='add_new'))
    under = 'ON' if settings.show_under_10k else 'OFF'
    fixes = 'ON' if settings.exclude_fixes else 'OFF'
    side = SIDE_LABELS.get(settings.side_filter or 'ALL', 'все')
    online = 'ON' if settings.only_online else 'OFF'
    rank = f'топ-{settings.max_rank}' if settings.max_rank else 'любое'
    removal = 'ON' if settings.removal_alerts else 'OFF'
    markup.row(types.InlineKeyboardButton(f'Ордера до 10к: {under}', callback_data='toggle_under10k'),
               types.InlineKeyboardButton(f'Исключать фиксы: {fixes}', callback_data='toggle_exclude_fixes'))
    markup.row(types.InlineKeyboardButton(f'Тип: {side}', callback_data='toggle_side'),
               types.InlineKeyboardButton(f'Только онлайн: {online}', callback_data='toggle_online'))
    markup.row(types.InlineKeyboardButton(f'Место в стакане: {rank}', callback_data='toggle_rank'),
               types.InlineKeyboardButton(f'Снятие объявлений: {removal}', callback_data='toggle_removal'))
    if settings.paused:
        markup.add(types.InlineKeyboardButton('▶️ Возобновить уведомления', callback_data='toggle_pause'))
    else:
        markup.add(types.InlineKeyboardButton('⏸ Пауза уведомлений', callback_data='toggle_pause'))
    if merchants:
        markup.add(types.InlineKeyboardButton('📊 Выгрузить Excel', callback_data='export_excel'))
    return markup


def settings_text(merchants, settings) -> str:
    if not merchants:
        text = 'Ты пока не добавил ни одного мерчанта.'
    else:
        text = f'⚙ Твои мерчанты ({len(merchants)}). ℹ️ — сводка по мерчанту, ❌ — убрать из списка.'
    if settings.paused:
        text += '\n\n⏸ Уведомления на паузе.'
    return text


def render_settings(chat_id, user_id, message_id=None):
    session = SessionLocal()
    try:
        settings = get_or_create_settings(session, user_id)
        merchants = get_merchants(session, user_id)
        text = settings_text(merchants, settings)
        markup = build_settings_markup(merchants, settings)
    finally:
        session.close()
    if message_id:
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
            return
        except ApiTelegramException as e:
            if 'message is not modified' in str(e):
                return
        except Exception as e:
            log.warning('edit_message_text не удался: %s', e)
    bot.send_message(chat_id, text, reply_markup=markup)


def offer_markup(nick: str, offer: dict) -> types.InlineKeyboardMarkup:
    key = remember_nick(nick)
    markup = types.InlineKeyboardMarkup()
    buttons = [types.InlineKeyboardButton('ℹ️ Подробнее', callback_data=f'info_h_{key}')]
    mask = offer.get('userMaskId')
    if mask:
        buttons.append(types.InlineKeyboardButton('🔗 Bybit', url=BYBIT_PROFILE_URL.format(mask=mask)))
    markup.row(*buttons)
    return markup


# === ТЕКСТЫ ===
def online_badge(offer: dict) -> str:
    if offer.get('isOnline'):
        return '🟢 онлайн'
    ago = fmt_ago(offer.get('lastLogoutTime'))
    return f'⚪️ офлайн, был {ago}' if ago else '⚪️ офлайн'


def merchant_stat_line(offer: dict, verbose: bool = False) -> str:
    stat = []
    if offer.get('recentOrderNum') is not None:
        s = f"Сделок за 30д: {offer.get('recentOrderNum')}"
        if offer.get('recentExecuteRate') is not None:
            s += f" ({offer.get('recentExecuteRate')}%)"
        stat.append(s)
    if any(t for t in (offer.get('authTag') or [])):
        stat.append('✅ проверен')
    if offer.get('userType') == 'ORG':
        stat.append('🏢 организация' if verbose else '🏢')
    return ' · '.join(stat)


def remark_line(offer: dict, limit: int) -> str:
    remark = (offer.get('remark') or '').strip().replace('\n', ' ')
    if not remark:
        return ''
    if len(remark) > limit:
        remark = remark[:limit].rstrip() + '…'
    return f'💬 <i>{esc(remark)}</i>'


def format_offer_message(offer: dict, trade_type: str, rank=None) -> str:
    nick = esc((offer.get('nickName') or '').strip())
    price_line = f'Курс: <b>{esc(offer.get("price"))} ₽</b>'
    if rank:
        price_line += f' · #{rank} в стакане'
    lines = [f'📢 <b>{nick}</b> · {SIDE_ICONS.get(trade_type, trade_type)} · {online_badge(offer)}',
             price_line]
    range_line = f'Диапазон: {fmt_amount(offer.get("minAmount"))} – {fmt_amount(offer.get("maxAmount"))} ₽'
    if offer.get('lastQuantity'):
        range_line += f' · доступно {fmt_amount(offer.get("lastQuantity"))} USDT'
    lines.append(range_line)
    pays = offer.get('payments') or []
    if pays:
        names = [payment_names.get(str(p), str(p)) for p in pays]
        lines.append('Оплата: ' + esc(', '.join(names)))
    stat = merchant_stat_line(offer)
    if stat:
        lines.append(stat)
    remark = remark_line(offer, REMARK_PREVIEW)
    if remark:
        lines.append(remark)
    return '\n'.join(lines)


def format_removed_message(entry: dict) -> str:
    return (f"⚫️ <b>{esc(entry['nick'])}</b> снял объявление · "
            f"{SIDE_ICONS.get(entry['trade_type'], entry['trade_type'])} {esc(entry['price'])} ₽")


def merchant_history(session, nick: str, days: int = 30) -> dict:
    """Сводка по нашему журналу: с какого дня знаем мерчанта, сколько изменений и коридор курса."""
    since = utcnow() - timedelta(days=days)
    nick_lower = nick.strip().lower()
    first = (session.query(func.min(OfferLog.timestamp))
             .filter(func.lower(OfferLog.nick_name) == nick_lower).scalar())
    rows = (session.query(OfferLog.trade_type, OfferLog.price, OfferLog.min_amount, OfferLog.max_amount)
            .filter(func.lower(OfferLog.nick_name) == nick_lower, OfferLog.timestamp >= since)
            .distinct().all())
    by_side = {}
    for trade_type, price, _mn, _mx in rows:
        if price:
            by_side.setdefault(trade_type, []).append(price)
    return {'first': first, 'changes': len(rows), 'by_side': by_side, 'days': days}


def build_info(session, user_id, nick: str):
    """Возвращает (текст, клавиатура) сводки по мерчанту."""
    key = nick.strip().lower()
    with scan_lock:
        ads = list(scan_state['by_nick'].get(key, []))
        scan_ts = scan_state['ts']
    tracked = find_tracked(session, user_id, key)
    if ads:
        display = (ads[0].get('nickName') or nick).strip()
    elif tracked:
        display = tracked.merchant_name
    else:
        display = nick.strip()
    remember_nick(display)

    lines = []
    if ads:
        head = ads[0]
        lines.append(f'ℹ️ <b>{esc(display)}</b> · {online_badge(head)}')
        stat = merchant_stat_line(head, verbose=True)
        if stat:
            lines.append(stat)
        lines.append('')
        lines.append('<b>Объявления сейчас:</b>')
        for ad in sorted(ads, key=lambda a: (a['_side'], a.get('_rank') or 0)):
            side = ad['_side']
            line = (f"{SIDE_ICONS.get(side, side)} <b>{esc(ad.get('price'))} ₽</b>"
                    f" · {fmt_amount(ad.get('minAmount'))} – {fmt_amount(ad.get('maxAmount'))}")
            if ad.get('_rank'):
                line += f" · #{ad.get('_rank')}"
            if ad.get('lastQuantity'):
                line += f" · {fmt_amount(ad.get('lastQuantity'))} USDT"
            lines.append(line)
        pref = head.get('tradingPreferenceSet') or {}
        reqs = []
        if pref.get('isKyc'):
            reqs.append('KYC')
        if pref.get('hasOrderFinishNumberDay30') and pref.get('orderFinishNumberDay30'):
            reqs.append(f"≥{pref.get('orderFinishNumberDay30')} сделок/30д")
        if pref.get('hasCompleteRateDay30') and pref.get('completeRateDay30'):
            reqs.append(f"исполнение ≥{pref.get('completeRateDay30')}%")
        if pref.get('hasRegisterTime') and pref.get('registerTimeThreshold'):
            reqs.append(f"аккаунту ≥{pref.get('registerTimeThreshold')} дн")
        if reqs:
            lines.append('Требования к контрагенту: ' + esc(', '.join(reqs)))
        remark = remark_line(head, 150)
        if remark:
            lines.append(remark)
    else:
        lines.append(f'ℹ️ <b>{esc(display)}</b>')
        lines.append('Сейчас объявлений в стакане нет.' if scan_ts
                     else 'Стакан ещё не загружен, попробуй через минуту.')

    hist = merchant_history(session, display)
    if hist['first']:
        lines.append('')
        parts = [f"в базе с {hist['first'].strftime('%d.%m.%Y')}"]
        if hist['changes']:
            parts.append(f"за {hist['days']} дн изменений: {hist['changes']}")
        lines.append('📒 ' + ', '.join(parts))
        corridor = []
        for side in ('BUY', 'SELL'):
            prices = hist['by_side'].get(side)
            if prices:
                lo, hi = min(prices), max(prices)
                icon = SIDE_ICONS[side].split()[0]
                corridor.append(f'{icon} {lo:g}–{hi:g}' if lo != hi else f'{icon} {lo:g}')
        if corridor:
            lines.append('Коридор курса за период: ' + ' · '.join(corridor) + ' ₽')
    elif ads:
        lines.append('')
        lines.append('📒 В нашей базе ещё не встречался.')

    nk = nick_key(display)
    markup = types.InlineKeyboardMarkup()
    if tracked:
        markup.row(types.InlineKeyboardButton('❌ Убрать из списка', callback_data=f'untrack_{nk}'),
                   types.InlineKeyboardButton('🔄 Обновить', callback_data=f'info_h_{nk}'))
    else:
        markup.row(types.InlineKeyboardButton('➕ Отслеживать', callback_data=f'track_{nk}'),
                   types.InlineKeyboardButton('🔄 Обновить', callback_data=f'info_h_{nk}'))
    mask = ads[0].get('userMaskId') if ads else None
    if mask:
        markup.add(types.InlineKeyboardButton('🔗 Открыть на Bybit', url=BYBIT_PROFILE_URL.format(mask=mask)))
    return '\n'.join(lines), markup


def send_info(chat_id, user_id, nick, message_id=None):
    session = SessionLocal()
    try:
        text, markup = build_info(session, user_id, nick)
    finally:
        session.close()
    if message_id:
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup,
                                  disable_web_page_preview=True)
            return
        except ApiTelegramException as e:
            if 'message is not modified' in str(e):
                return
        except Exception as e:
            log.warning('edit_message_text (info) не удался: %s', e)
    bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)


# === ФИЛЬТРЫ И СОПОСТАВЛЕНИЕ ===
def offer_passes(settings, offer: dict, trade_type: str, rank=None) -> bool:
    """Применяет персональные фильтры пользователя к офферу."""
    if settings is None:
        return True
    if settings.paused:
        return False
    side = settings.side_filter or 'ALL'
    if side != 'ALL' and side != trade_type:
        return False
    max_amount = to_float(offer.get('maxAmount'))
    min_amount = to_float(offer.get('minAmount'))
    if settings.show_under_10k is False and max_amount < 10000:
        return False
    if settings.exclude_fixes and abs(min_amount - max_amount) < 1e-9:
        return False
    if getattr(settings, 'only_online', False) and not offer.get('isOnline'):
        return False
    max_rank = getattr(settings, 'max_rank', 0) or 0
    if max_rank and rank and rank > max_rank:
        return False
    return True


def load_tracking(session):
    """{nick_lower: set(user_id)}, {user_id: username}"""
    rows = (session.query(TrackedMerchant.user_id, TrackedMerchant.merchant_name, TrackedMerchant.username)
            .filter(TrackedMerchant.merchant_name != '').all())
    tracked, usernames = {}, {}
    for user_id, name, username in rows:
        if not name:
            continue
        tracked.setdefault(name.strip().lower(), set()).add(user_id)
        if username:
            usernames[user_id] = username
    return tracked, usernames


def process_offers(session, results: dict, seen: dict, silent: bool = False, ranks: dict = None):
    """Сопоставляет офферы с подписками. Возвращает список (user_id, текст, markup) и число новых офферов.

    seen: {offer_id_str: offer_hash} — обновляется на месте; новые записи добавляются в SentOffer.
    silent: первый цикл после старта — только запомнить текущие объявления отслеживаемых мерчантов,
    ничего не отправлять (иначе после простоя прилетит шквал давно висящих объявлений).
    """
    ranks = ranks if ranks is not None else compute_ranks(results)
    tracked, usernames = load_tracking(session)
    if not tracked:
        return [], 0
    settings_map = {s.user_id: s for s in session.query(UserSettings).all()}

    outgoing = []
    new_offers = 0
    for trade_type, items in results.items():
        if not items:
            continue
        for offer in items:
            nick_clean = (offer.get('nickName') or '').strip()
            users = tracked.get(nick_clean.lower())
            if not users:
                continue
            offer_id_str = f"{trade_type}_{offer.get('id', '')}"
            offer_hash = compute_offer_hash(offer)
            if seen.get(offer_id_str) == offer_hash:
                continue
            if silent:
                seen[offer_id_str] = offer_hash
                session.add(SentOffer(offer_id=offer_id_str, offer_hash=offer_hash))
                new_offers += 1
                continue

            matched = []
            text = None
            markup = None
            rank = ranks.get(trade_type, {}).get(offer.get('id'))
            for user_id in sorted(users):
                if not offer_passes(settings_map.get(user_id), offer, trade_type, rank):
                    continue
                if text is None:
                    text = format_offer_message(offer, trade_type, rank)
                    markup = offer_markup(nick_clean, offer)
                outgoing.append((user_id, text, markup))
                matched.append(user_id)
                try:
                    session.add(OfferLog(user_id=user_id, nick_name=nick_clean, trade_type=trade_type,
                                         price=float(offer.get('price')),
                                         min_amount=float(offer.get('minAmount')),
                                         max_amount=float(offer.get('maxAmount'))))
                except (TypeError, ValueError) as e:
                    log.error('Ошибка преобразования чисел в логах от %s: %s', nick_clean, e)
                uname = usernames.get(user_id)
                log.info('Мерчант %s (%s) — отправляем %s (%s)', nick_clean, trade_type,
                         f'@{uname}' if uname else '—', user_id)
            if matched:
                seen[offer_id_str] = offer_hash
                session.add(SentOffer(offer_id=offer_id_str, offer_hash=offer_hash))
                new_offers += 1
    return outgoing, new_offers


def update_active(active: dict, results: dict, tracked_nicks) -> list:
    """Следит за объявлениями отслеживаемых мерчантов; возвращает те, что пропали из стакана.

    active: {offer_id_str: {'nick','trade_type','price','missing'}} — обновляется на месте.
    Сторона, которую не удалось получить (None), не оценивается.
    """
    removed = []
    for trade_type, items in results.items():
        if items is None:
            continue
        current = set()
        for offer in items:
            nick = (offer.get('nickName') or '').strip()
            if nick.lower() not in tracked_nicks:
                continue
            oid = f"{trade_type}_{offer.get('id', '')}"
            current.add(oid)
            active[oid] = {'nick': nick, 'trade_type': trade_type, 'price': offer.get('price'), 'missing': 0}
        for oid, entry in list(active.items()):
            if entry['trade_type'] != trade_type or oid in current:
                continue
            entry['missing'] += 1
            if entry['missing'] >= REMOVAL_CONFIRM_CYCLES:
                removed.append(active.pop(oid))
    return removed


def removal_messages(session, removed: list) -> list:
    """(user_id, text, None) для пользователей, включивших уведомления о снятии."""
    if not removed:
        return []
    tracked, _ = load_tracking(session)
    settings_map = {s.user_id: s for s in session.query(UserSettings).all()}
    out = []
    for entry in removed:
        for user_id in sorted(tracked.get(entry['nick'].lower(), set())):
            s = settings_map.get(user_id)
            if not s or not s.removal_alerts or s.paused:
                continue
            side = s.side_filter or 'ALL'
            if side != 'ALL' and side != entry['trade_type']:
                continue
            out.append((user_id, format_removed_message(entry), None))
            log.info('Мерчант %s (%s) снял объявление — сообщаем %s', entry['nick'], entry['trade_type'], user_id)
    return out


# === КОМАНДЫ ===
@bot.message_handler(commands=['start'])
def send_welcome(message):
    session = SessionLocal()
    try:
        ensure_user(session, message.from_user)
    finally:
        session.close()
    user_states.pop(message.from_user.id, None)
    welcome_text = (
        '👋 Привет! Этот бот следит за мерчантами на Bybit P2P (USDT/RUB) и присылает уведомление, '
        'когда они выставляют или меняют объявления.\n\n'
        '⚙ /settings — список мерчантов, фильтры, пауза\n'
        'ℹ️ /info ник — сводка по мерчанту: онлайн, объявления, сделки, история\n'
        '📊 /stats — статистика уведомлений\n'
        '❓ /help — как работают фильтры'
    )
    if ADMIN_CONTACT:
        welcome_text += f'\n\n💡 Вопросы и предложения: {esc(ADMIN_CONTACT)}'
    bot.send_message(message.chat.id, welcome_text)
    notify_admin(f"👤 @{message.from_user.username or '—'} ({message.from_user.id}) запустил бота")


@bot.message_handler(commands=['help'])
def send_help(message):
    bot.send_message(message.chat.id, help_text())


def help_text() -> str:
    text = (
        'ℹ️ <b>Как это работает</b>\n'
        f'Раз в {CHECK_INTERVAL} секунд бот сверяет стакан Bybit P2P (USDT/RUB) с твоим списком мерчантов. '
        'Уведомление приходит, когда объявление появилось или изменилось (курс, диапазон). '
        'Под каждым уведомлением кнопки: ℹ️ Подробнее — сводка по мерчанту, 🔗 Bybit — его профиль.\n\n'
        '<b>Фильтры в /settings</b>\n'
        '• <i>Ордера до 10к</i> — OFF скрывает объявления с максимумом меньше 10 000 ₽\n'
        '• <i>Исключать фиксы</i> — ON скрывает объявления, где мин = макс\n'
        '• <i>Тип</i> — все / только покупка / только продажа\n'
        '• <i>Только онлайн</i> — ON скрывает объявления мерчантов, которые сейчас офлайн\n'
        '• <i>Место в стакане</i> — топ-10/30/100: только объявления не ниже этого места '
        'в списке Bybit (место считается по всему стакану, включая мусорные объявления)\n'
        '• <i>Снятие объявлений</i> — ON присылает ⚫️, когда мерчант убрал объявление\n'
        '• <i>Пауза</i> — мерчанты остаются, уведомления не приходят\n\n'
        '/info ник — сводка по любому мерчанту, даже не из списка\n'
        '/pause — пауза/возобновление, /stats — статистика'
    )
    if ADMIN_CONTACT:
        text += f'\nВопросы: {esc(ADMIN_CONTACT)}'
    return text


@bot.message_handler(commands=['settings'])
def settings_menu(message):
    session = SessionLocal()
    try:
        ensure_user(session, message.from_user)
    finally:
        session.close()
    user_states.pop(message.from_user.id, None)
    render_settings(message.chat.id, message.from_user.id)


@bot.message_handler(commands=['info'])
def info_command(message):
    user_id = message.from_user.id
    parts = (message.text or '').split(maxsplit=1)
    nick = parts[1].strip() if len(parts) > 1 else ''
    if nick:
        send_info(message.chat.id, user_id, nick)
        return
    session = SessionLocal()
    try:
        ensure_user(session, message.from_user)
        merchants = get_merchants(session, user_id)
    finally:
        session.close()
    if not merchants:
        bot.send_message(message.chat.id, 'Напиши <code>/info ник</code> — покажу сводку по любому мерчанту.')
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[types.InlineKeyboardButton(f'ℹ️ {m.merchant_name}', callback_data=f'info_id_{m.id}')
                 for m in merchants])
    bot.send_message(message.chat.id,
                     'Выбери мерчанта или напиши <code>/info ник</code> — можно любого, не только из списка.',
                     reply_markup=markup)


@bot.message_handler(commands=['pause'])
def pause_command(message):
    session = SessionLocal()
    try:
        settings = ensure_user(session, message.from_user)
        settings.paused = not settings.paused
        session.commit()
        paused = settings.paused
    finally:
        session.close()
    bot.send_message(message.chat.id,
                     '⏸ Уведомления на паузе. /pause — чтобы возобновить.' if paused
                     else '▶️ Уведомления возобновлены.')


@bot.message_handler(commands=['stats'])
def stats_command(message):
    session = SessionLocal()
    try:
        user_id = message.from_user.id
        merchants = get_merchants(session, user_id)
        today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        day_ago = utcnow() - timedelta(days=1)
        total = session.query(func.count(OfferLog.id)).filter(OfferLog.user_id == user_id).scalar() or 0
        today = (session.query(func.count(OfferLog.id))
                 .filter(OfferLog.user_id == user_id, OfferLog.timestamp >= today_start).scalar() or 0)
        per_merchant = (session.query(OfferLog.nick_name, func.count(OfferLog.id))
                        .filter(OfferLog.user_id == user_id, OfferLog.timestamp >= day_ago)
                        .group_by(OfferLog.nick_name).order_by(func.count(OfferLog.id).desc()).limit(10).all())
        last = (session.query(OfferLog).filter(OfferLog.user_id == user_id)
                .order_by(OfferLog.timestamp.desc()).first())
    finally:
        session.close()
    lines = ['📊 <b>Статистика</b>',
             f'Мерчантов в списке: {len(merchants)}',
             f'Уведомлений сегодня (UTC): {today}',
             f'Уведомлений всего: {total}']
    if last and last.timestamp:
        lines.append(f'Последнее: {last.timestamp.strftime("%d.%m.%Y %H:%M")} UTC — {esc(last.nick_name)}')
    markup = None
    if per_merchant:
        lines.append('\nЗа последние 24 часа:')
        lines += [f'• {esc(nick)}: {cnt}' for nick, cnt in per_merchant]
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(*[types.InlineKeyboardButton(f'ℹ️ {nick}', callback_data=f'info_h_{remember_nick(nick)}')
                     for nick, _ in per_merchant[:6]])
    bot.send_message(message.chat.id, '\n'.join(lines), reply_markup=markup)


@bot.message_handler(commands=['cancel'])
def cancel_command(message):
    if user_states.pop(message.from_user.id, None):
        bot.send_message(message.chat.id, 'Отменено.')
    else:
        bot.send_message(message.chat.id, 'Нечего отменять. /settings — меню.')


# === CALLBACKS ===
def resolve_nick(session, user_id, data: str):
    """info_h_<key> / info_id_<row> / track_<key> / untrack_<key> -> ник или None."""
    if data.startswith('info_id_'):
        try:
            row = session.query(TrackedMerchant).filter_by(id=int(data[8:]), user_id=user_id).first()
        except ValueError:
            return None
        return row.merchant_name if row else None
    key = data.split('_')[-1]
    nick = known_nicks.get(key)
    if nick:
        return nick
    # после рестарта память пуста — ищем среди мерчантов пользователя и в стакане
    for m in get_merchants(session, user_id):
        if nick_key(m.merchant_name) == key:
            return m.merchant_name
    with scan_lock:
        for nick_lower, ads in scan_state['by_nick'].items():
            if nick_key(nick_lower) == key:
                return ads[0].get('nickName', nick_lower)
    return None


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data or ''
    session = SessionLocal()
    try:
        if data.startswith('del_') or data.startswith('delete_'):
            if data.startswith('del_'):
                try:
                    row_id = int(data[4:])
                except ValueError:
                    bot.answer_callback_query(call.id, 'Некорректная кнопка')
                    return
                row = session.query(TrackedMerchant).filter_by(id=row_id, user_id=user_id).first()
            else:  # старые сообщения с именем в callback_data
                row = find_tracked(session, user_id, data[len('delete_'):])
            if not row:
                bot.answer_callback_query(call.id, 'Мерчант уже удалён', show_alert=True)
                render_settings(chat_id, user_id, message_id)
                return
            nickname = row.merchant_name
            session.delete(row)
            session.commit()
            bot.answer_callback_query(call.id, f'Удалён {nickname}')
            render_settings(chat_id, user_id, message_id)
            notify_admin(f"➖ @{call.from_user.username or '—'} ({user_id}) удалил мерча: {esc(nickname)}")

        elif data.startswith('info_'):
            nick = resolve_nick(session, user_id, data)
            if not nick:
                bot.answer_callback_query(call.id, 'Не нашёл мерчанта, напиши /info ник', show_alert=True)
                return
            bot.answer_callback_query(call.id)
            # Из уведомления/настроек — новым сообщением; из самой сводки (🔄) — редактируем на месте
            edit = data.startswith('info_h_') and (call.message.text or '').startswith('ℹ️')
            send_info(chat_id, user_id, nick, message_id if edit else None)

        elif data.startswith('track_') or data.startswith('untrack_'):
            nick = resolve_nick(session, user_id, data)
            if not nick:
                bot.answer_callback_query(call.id, 'Не нашёл мерчанта, напиши /info ник', show_alert=True)
                return
            ensure_user(session, call.from_user)
            row = find_tracked(session, user_id, nick)
            if data.startswith('track_'):
                if not row:
                    session.add(TrackedMerchant(user_id=user_id, merchant_name=nick,
                                                username=call.from_user.username))
                    session.commit()
                    notify_admin(f"➕ @{call.from_user.username or '—'} ({user_id}) добавил мерча: {esc(nick)}")
                bot.answer_callback_query(call.id, f'Отслеживаю {nick}')
            else:
                if row:
                    session.delete(row)
                    session.commit()
                    notify_admin(f"➖ @{call.from_user.username or '—'} ({user_id}) удалил мерча: {esc(nick)}")
                bot.answer_callback_query(call.id, f'Убрал {nick}')
            send_info(chat_id, user_id, nick, message_id)

        elif data == 'add_new':
            user_states[user_id] = 'awaiting_nickname'
            bot.answer_callback_query(call.id)
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton('Отмена', callback_data='cancel_add'))
            bot.send_message(chat_id, '✏ Введи никнейм мерчанта, которого хочешь добавить '
                                      '(неважно, в каком регистре):', reply_markup=markup)

        elif data == 'cancel_add':
            user_states.pop(user_id, None)
            bot.answer_callback_query(call.id, 'Отменено')
            try:
                bot.edit_message_text(chat_id=chat_id, message_id=message_id, text='Добавление отменено.')
            except Exception:
                pass

        elif data == 'export_excel':
            export_excel(call, session)

        elif data.startswith('toggle_'):
            settings = get_or_create_settings(session, user_id)
            if data == 'toggle_under10k':
                settings.show_under_10k = not settings.show_under_10k
                answer = f"Фильтр до 10к: {'ON' if settings.show_under_10k else 'OFF'}"
            elif data == 'toggle_exclude_fixes':
                settings.exclude_fixes = not settings.exclude_fixes
                answer = f"Исключать фиксы: {'ON' if settings.exclude_fixes else 'OFF'}"
            elif data == 'toggle_side':
                current = settings.side_filter or 'ALL'
                idx = SIDE_FILTERS.index(current) if current in SIDE_FILTERS else 0
                settings.side_filter = SIDE_FILTERS[(idx + 1) % len(SIDE_FILTERS)]
                answer = f'Тип объявлений: {SIDE_LABELS[settings.side_filter]}'
            elif data == 'toggle_online':
                settings.only_online = not settings.only_online
                answer = f"Только онлайн: {'ON' if settings.only_online else 'OFF'}"
            elif data == 'toggle_rank':
                current = settings.max_rank or 0
                idx = RANK_STEPS.index(current) if current in RANK_STEPS else 0
                settings.max_rank = RANK_STEPS[(idx + 1) % len(RANK_STEPS)]
                answer = f"Место в стакане: {'топ-%d' % settings.max_rank if settings.max_rank else 'любое'}"
            elif data == 'toggle_removal':
                settings.removal_alerts = not settings.removal_alerts
                answer = f"Снятие объявлений: {'ON' if settings.removal_alerts else 'OFF'}"
            elif data == 'toggle_pause':
                settings.paused = not settings.paused
                answer = 'Уведомления на паузе' if settings.paused else 'Уведомления возобновлены'
            else:
                bot.answer_callback_query(call.id)
                return
            session.commit()
            bot.answer_callback_query(call.id, answer)
            render_settings(chat_id, user_id, message_id)
        else:
            bot.answer_callback_query(call.id)
    except Exception as e:
        log.exception('Ошибка в обработке callback %r: %s', data, e)
        try:
            bot.answer_callback_query(call.id, 'Ошибка, попробуй ещё раз')
        except Exception:
            pass
    finally:
        session.close()


def export_excel(call, session):
    user_id = call.from_user.id
    logs = (session.query(OfferLog).filter_by(user_id=user_id)
            .order_by(OfferLog.timestamp.desc()).limit(EXPORT_ROWS_LIMIT).all())
    if not logs:
        bot.answer_callback_query(call.id, 'Нет данных для экспорта.', show_alert=True)
        return
    bot.answer_callback_query(call.id, 'Готовлю файл…')
    data = [{
        'Дата (UTC)': l.timestamp.strftime('%Y-%m-%d %H:%M') if l.timestamp else '',
        'Мерчант': l.nick_name,
        'Тип': l.trade_type,
        'Курс': l.price,
        'Мин. сумма': l.min_amount,
        'Макс. сумма': l.max_amount,
    } for l in logs]
    df = pd.DataFrame(data)
    export_path = os.path.join(tempfile.gettempdir(), f'merchant_logs_{user_id}.xlsx')
    df.to_excel(export_path, index=False)
    caption = f'📄 Журнал наблюдений: {len(logs)} записей'
    if len(logs) >= EXPORT_ROWS_LIMIT:
        caption += f' (показаны последние {EXPORT_ROWS_LIMIT})'
    try:
        with open(export_path, 'rb') as f:
            bot.send_document(call.message.chat.id, f, caption=caption, visible_file_name='merchant_logs.xlsx')
    finally:
        try:
            os.remove(export_path)
        except OSError:
            pass


# === ВВОД НИКНЕЙМА ===
@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == 'awaiting_nickname', content_types=['text'])
def handle_merchant_input(message):
    user_id = message.from_user.id
    nickname = (message.text or '').strip()
    if not nickname or nickname.startswith('/') or nickname.lower() in ('отмена', 'cancel'):
        user_states.pop(user_id, None)
        bot.reply_to(message, 'Добавление отменено.')
        return
    if len(nickname) > MAX_MERCHANT_NAME or any(ch in nickname for ch in '\n\r\t'):
        bot.reply_to(message, f'Никнейм слишком длинный или содержит недопустимые символы '
                              f'(максимум {MAX_MERCHANT_NAME}). Попробуй ещё раз или /cancel.')
        return
    session = SessionLocal()
    try:
        ensure_user(session, message.from_user)
        exists = find_tracked(session, user_id, nickname)
        if exists:
            user_states.pop(user_id, None)
            bot.reply_to(message, f'Мерчант {esc(exists.merchant_name)} уже в списке.')
            return
        session.add(TrackedMerchant(user_id=user_id, merchant_name=nickname, username=message.from_user.username))
        session.commit()
    finally:
        session.close()
    user_states.pop(user_id, None)
    key = remember_nick(nickname)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton('ℹ️ Сводка по мерчанту', callback_data=f'info_h_{key}'))
    bot.reply_to(message, f'✅ Добавлен мерчант <b>{esc(nickname)}</b>', reply_markup=markup)
    notify_admin(f"➕ @{message.from_user.username or '—'} ({user_id}) добавил мерча: {esc(nickname)}")


# === ПОТОК ПРОВЕРКИ ===
def checker_loop():
    session = SessionLocal()
    try:
        seen = load_recent_sent(session, SENT_TTL_DAYS)
    finally:
        session.close()
    log.info('Загружено %d отправленных офферов за %d дней', len(seen), SENT_TTL_DAYS)

    failures = 0
    alerted = False
    last_cleanup = time.time()
    silent = True  # первый успешный цикл — тихая фиксация текущего стакана
    active = {}    # объявления отслеживаемых мерчантов, которые сейчас висят в стакане
    while True:
        started = time.time()
        try:
            results = bybit_check_all('USDT')
            if all(v is None for v in results.values()):
                failures += 1
                if failures == API_ALERT_AFTER and not alerted:
                    notify_admin(f'⚠️ Bybit API не отвечает уже ~{failures * CHECK_INTERVAL // 60} мин. '
                                 f'Уведомления не приходят.')
                    alerted = True
            else:
                if alerted:
                    notify_admin(f'✅ Bybit API снова отвечает (после {failures} неудачных циклов).')
                failures = 0
                alerted = False
                ranks = compute_ranks(results)
                update_scan_state(results, ranks)
                session = SessionLocal()
                try:
                    outgoing, new_offers = process_offers(session, results, seen, silent=silent, ranks=ranks)
                    try:
                        session.commit()
                    except Exception as e:
                        session.rollback()
                        log.error('Ошибка при сохранении логов или SentOffer: %s', e)
                    tracked, _ = load_tracking(session)
                    removed = update_active(active, results, tracked)
                    if silent:
                        log.info('Стартовая фиксация: %d объявлений отслеживаемых мерчантов (BUY %s / SELL %s)',
                                 new_offers, len(results.get('BUY') or []), len(results.get('SELL') or []))
                        silent = False
                    else:
                        outgoing += removal_messages(session, removed)
                        if new_offers or removed:
                            log.info('Новых офферов: %d, снято: %d, сообщений в очередь: %d',
                                     new_offers, len(removed), len(outgoing))
                    for item in outgoing:
                        send_queue.put(item)
                finally:
                    session.close()

            if time.time() - last_cleanup > CLEANUP_INTERVAL:
                session = SessionLocal()
                try:
                    deleted = cleanup_sent_offers(session, SENT_TTL_DAYS)
                    seen = load_recent_sent(session, SENT_TTL_DAYS)
                    log.info('Чистка sent_offers: удалено %d, в памяти %d', deleted, len(seen))
                except Exception as e:
                    log.error('Ошибка чистки sent_offers: %s', e)
                finally:
                    session.close()
                last_cleanup = time.time()
        except Exception as e:
            log.exception('Неожиданная ошибка checker_loop: %s', e)
        time.sleep(max(1.0, CHECK_INTERVAL - (time.time() - started)))


# === ОТПРАВКА ===
def _forget_user(user_id):
    session = SessionLocal()
    try:
        session.query(TrackedMerchant).filter_by(user_id=user_id).delete()
        session.query(UserSettings).filter_by(user_id=user_id).delete()
        session.commit()
    except Exception as e:
        log.exception('Ошибка при удалении user_id=%s из БД: %s', user_id, e)
    finally:
        session.close()


def sender_loop():
    user_last_sent = {}
    while True:
        try:
            user_id, text, markup = send_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            # антифлуд: не чаще 1 сообщения в секунду на пользователя
            delta = time.time() - user_last_sent.get(user_id, 0)
            if delta < 1:
                time.sleep(1 - delta)
            bot.send_message(user_id, text, reply_markup=markup, disable_web_page_preview=True)
            user_last_sent[user_id] = time.time()
            time.sleep(0.12)  # общий лимит ~8 сообщений/сек
        except ApiTelegramException as e:
            code = getattr(e, 'error_code', None)
            err = str(e)
            if code == 403 or 'bot was blocked' in err or 'user is deactivated' in err:
                log.warning('User %s: бот заблокирован / недоступен. Удаляю подписки.', user_id)
                _forget_user(user_id)
            elif code == 429:
                retry = 3
                try:
                    retry = int((e.result_json or {}).get('parameters', {}).get('retry_after', 3))
                except Exception:
                    pass
                log.warning('User %s: rate limit от Telegram, сплю %s сек', user_id, retry)
                time.sleep(retry)
                send_queue.put((user_id, text, markup))
            else:
                log.error('Telegram API ошибка при отправке user_id=%s: %s', user_id, err)
        except Exception as e:
            log.exception('Неожиданная ошибка sender_loop для user_id=%s: %s', user_id, e)
            time.sleep(1)


def load_payment_names():
    try:
        names = fetch_payment_names('RUB')
        if names:
            payment_names.update(names)
            log.info('Справочник способов оплаты: %d записей', len(names))
    except Exception as e:
        log.warning('Не удалось загрузить справочник способов оплаты: %s', e)


BOT_COMMANDS = [
    ('settings', 'Мерчанты, фильтры, пауза'),
    ('info', 'Сводка по мерчанту: /info ник'),
    ('stats', 'Статистика уведомлений'),
    ('pause', 'Пауза / возобновление уведомлений'),
    ('help', 'Как работают фильтры'),
]


def register_commands():
    """Меню команд в Telegram — чтобы не настраивать его вручную в @BotFather."""
    try:
        bot.set_my_commands([types.BotCommand(cmd, desc) for cmd, desc in BOT_COMMANDS])
    except Exception as e:
        log.warning('Не удалось зарегистрировать меню команд: %s', e)


def main():
    init_db()
    register_commands()
    load_payment_names()
    threading.Thread(target=checker_loop, daemon=True, name='checker').start()
    threading.Thread(target=sender_loop, daemon=True, name='sender').start()
    log.info('Бот запущен')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=25)
        except Exception as e:
            log.exception('Polling упал (сеть/Telegram): %s', e)
            time.sleep(5)


if __name__ == '__main__':
    main()
