HEADERS_MAXI = {
    'Accept': '*/*',
    'Accept-Encoding': 'gzip, deflate, br',
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'content-type': 'application/json',
    'Pragma': 'no-cache',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36'
}

# Известные коды способов оплаты Bybit (дополняются справочником с API при старте)
PAYMENT_NAMES_FALLBACK = {
    "14": "Bank Transfer",
    "18": "Cash Deposit",
    "40": "Mobile Top-up",
    "90": "Наличные",
    "64": "Raiffeisenbank",
    "382": "СБП",
    "581": "Т-Банк",
    "582": "Сбербанк",
    "583": "Альфа-Банк",
}

TRADE_TYPE_DIC = {
    "BUY": "1",  # Покупка
    "SELL": "0"  # Продажа
}
