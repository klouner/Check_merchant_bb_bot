"""Настройки бота.

Порядок: переменные окружения → файл .env рядом с кодом → config.py (старый способ, для совместимости).
Шаблон — .env.example.
"""
import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Уже заданные переменные окружения не перезаписываются
load_dotenv(os.path.join(BASE_DIR, '.env'))

try:
    import config as _legacy  # noqa: F401 — config.py из старых установок
except ImportError:
    _legacy = None


def _get(name: str, default: str = '') -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == '':
        value = getattr(_legacy, name, None)
    if value is None or str(value).strip() == '':
        return default
    return str(value).strip()


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


# Токен от @BotFather — обязателен
BOT_TOKEN = _get('BOT_TOKEN')
# Чат для служебных уведомлений (новые пользователи, добавление/удаление мерчантов, падение Bybit API).
# Пусто — служебные уведомления не отправляются.
ADMIN_GROUP_ID = _get('ADMIN_GROUP_ID')
# Контакт для вопросов, показывается в /start и /help. Пусто — строка не выводится.
ADMIN_CONTACT = _get('ADMIN_CONTACT')
# Секунд между опросами Bybit
CHECK_INTERVAL = max(3, _get_int('CHECK_INTERVAL', 10))
# Папка для базы и логов
DATA_DIR = _get('DATA_DIR', BASE_DIR)
DB_PATH = _get('MERCHANTS_DB_PATH', os.path.join(DATA_DIR, 'merchants.db'))
LOG_PATH = _get('BOT_LOG_PATH', os.path.join(DATA_DIR, 'bot.log'))
