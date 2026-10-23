"""Запросы к публичному P2P-API Bybit."""
import logging
from typing import Dict, List, Optional

import requests

from const import HEADERS_MAXI, TRADE_TYPE_DIC

API_URL = 'https://api2.bybit.com/fiat/otc/item/online'
PAYMENTS_URL = 'https://api2.bybit.com/fiat/otc/configuration/queryAllPaymentList'
# Bybit отдаёт максимум 300 объявлений на страницу (400+ -> «Invalid request parameter»)
PAGE_SIZE = 300
MAX_PAGES = 15
TIMEOUT = 15

log = logging.getLogger(__name__)


def _headers() -> dict:
    headers = {k: v for k, v in HEADERS_MAXI.items() if k not in ('Content-Length', 'TE', 'Host')}
    headers['Origin'] = 'https://www.bybit.com'
    headers['Referer'] = 'https://www.bybit.com/'
    return headers


def fetch_side(asset: str, fiat: str, trade_type: str) -> List[dict]:
    """Собирает все страницы объявлений одной стороны. Бросает исключение, если API недоступен."""
    side = TRADE_TYPE_DIC.get(trade_type, '1')
    items: List[dict] = []
    seen_ids = set()
    page = 1
    total = 0
    while page <= MAX_PAGES:
        payload = {
            'tokenId': asset,
            'currencyId': fiat,
            'side': side,
            'size': str(PAGE_SIZE),
            'page': str(page),
        }
        response = requests.post(API_URL, headers=_headers(), json=payload, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get('ret_code') != 0:
            code = data.get('ret_code') if isinstance(data, dict) else None
            msg = data.get('ret_msg') if isinstance(data, dict) else data
            raise RuntimeError(f'Bybit ret_code={code} ret_msg={msg}')
        result = data.get('result') or {}
        page_items = result.get('items') or []
        for it in page_items:
            if it.get('id') not in seen_ids:
                seen_ids.add(it.get('id'))
                items.append(it)
        try:
            total = int(result.get('count') or 0)
        except (TypeError, ValueError):
            total = 0
        if not page_items or len(items) >= total or len(page_items) < PAGE_SIZE:
            break
        page += 1
    log.debug('Bybit %s: %d объявлений (count=%s, страниц=%d)', trade_type, len(items), total, page)
    return items


def bybit_check_all(asset: str = 'USDT', fiat: str = 'RUB') -> Dict[str, Optional[List[dict]]]:
    """Возвращает {'BUY': [...], 'SELL': [...]}; значение None означает ошибку запроса стороны."""
    results: Dict[str, Optional[List[dict]]] = {}
    for trade_type in ('BUY', 'SELL'):
        try:
            results[trade_type] = fetch_side(asset, fiat, trade_type)
        except Exception as e:  # сеть, ret_code, JSON
            log.warning('Ошибка bybit_check_all (%s): %s', trade_type, e)
            results[trade_type] = None
    return results


def fetch_payment_names(fiat: str = 'RUB') -> Dict[str, str]:
    """Справочник {paymentType: название} для расшифровки способов оплаты в объявлении."""
    headers = _headers()
    headers['lang'] = 'ru-RU'
    headers['Accept-Language'] = 'ru-RU,ru;q=0.9'
    response = requests.post(PAYMENTS_URL, headers=headers, json={'currencyId': fiat}, timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json() or {}
    result = data.get('result') or {}
    config = result.get('paymentConfigVo') or []
    names = {}
    for item in config:
        ptype = str(item.get('paymentType', '')).strip()
        name = (item.get('paymentName') or '').strip()
        if ptype and name:
            names[ptype] = name
    return names
