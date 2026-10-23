"""Офлайн-тесты: без реального Telegram и Bybit."""
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Отдельная база на прогон тестов — до импорта database
_tmpdir = tempfile.mkdtemp()
os.environ['MERCHANTS_DB_PATH'] = os.path.join(_tmpdir, 'test.db')
os.environ['BOT_LOG_PATH'] = os.path.join(_tmpdir, 'test.log')
os.environ.setdefault('BOT_TOKEN', '123456:TEST-TOKEN')

import checker  # noqa: E402
import database  # noqa: E402
import main  # noqa: E402
from database import OfferLog, SentOffer, SessionLocal, TrackedMerchant, UserSettings  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    database.init_db()
    s = SessionLocal()
    for model in (TrackedMerchant, OfferLog, SentOffer, UserSettings):
        s.query(model).delete()
    s.commit()
    s.close()
    main.update_scan_state({'BUY': [], 'SELL': []})
    main.scan_state['ts'] = 0.0
    yield


def offer(**kw):
    base = {'id': '1000000000000000001', 'nickName': 'Test_Merch<O>', 'price': '78.78', 'minAmount': '500.00',
            'maxAmount': '160000.00', 'userId': '0', 'lastQuantity': '32016.1424',
            'payments': ['40', '90', '14'], 'recentOrderNum': 951, 'recentExecuteRate': 99,
            'orderNum': 4120, 'finishNum': 4100, 'isOnline': True, 'authTag': ['GA'], 'userType': 'PERSONAL',
            'userMaskId': 's0000000000000000000000000000test', 'remark': 'Только <Сбер>\nбыстро ' + 'x' * 100,
            'tradingPreferenceSet': {'isKyc': 1, 'hasOrderFinishNumberDay30': 1, 'orderFinishNumberDay30': 5}}
    base.update(kw)
    return base


def make_settings(**kw):
    s = UserSettings(user_id=1, show_under_10k=True, exclude_fixes=False, paused=False, side_filter='ALL',
                     only_online=False, max_rank=0, removal_alerts=False)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# --- форматирование ---
def test_format_escapes_html_and_underscores():
    text = main.format_offer_message(offer(), 'BUY')
    assert '<b>Test_Merch&lt;O&gt;</b>' in text
    assert '🟢 Покупка' in text and '🟢 онлайн' in text
    assert 'Диапазон: 500 – 160 000 ₽ · доступно 32 016.14 USDT' in text
    assert 'Mobile Top-up' in text and 'Наличные' in text
    assert 'Сделок за 30д: 951 (99%) · ✅ проверен' in text
    assert '💬 <i>Только &lt;Сбер&gt; быстро' in text and text.endswith('…</i>')
    assert text.count('x') <= main.REMARK_PREVIEW
    assert 'в стакане' not in text  # место не передано
    assert len(text.split('\n')) == 6


def test_format_rank_and_offline():
    text = main.format_offer_message(offer(isOnline=False, lastLogoutTime=int(time.time()) - 7200),
                                     'SELL', rank=12)
    assert 'Курс: <b>78.78 ₽</b> · #12 в стакане' in text
    assert '⚪️ офлайн, был 2 ч назад' in text
    assert '🔴 Продажа' in text


def test_offer_markup_has_info_and_bybit_link():
    markup = main.offer_markup('Test_Merch<O>', offer())
    buttons = markup.keyboard[0]
    assert buttons[0].callback_data == 'info_h_' + main.nick_key('test_merch<o>')
    assert buttons[1].url.endswith('/profile/s0000000000000000000000000000test/USDT/RUB/item')
    assert main.known_nicks[main.nick_key('TEST_MERCH<o>')] == 'Test_Merch<O>'


# --- фильтры ---
def test_offer_passes_filters():
    assert main.offer_passes(make_settings(), offer(), 'BUY')
    assert not main.offer_passes(make_settings(paused=True), offer(), 'BUY')
    assert not main.offer_passes(make_settings(side_filter='SELL'), offer(), 'BUY')
    assert main.offer_passes(make_settings(side_filter='SELL'), offer(), 'SELL')
    assert not main.offer_passes(make_settings(show_under_10k=False), offer(maxAmount='9999'), 'BUY')
    assert main.offer_passes(make_settings(show_under_10k=False), offer(maxAmount='10000'), 'BUY')
    assert not main.offer_passes(make_settings(exclude_fixes=True), offer(minAmount='5000', maxAmount='5000'), 'BUY')
    assert main.offer_passes(make_settings(exclude_fixes=True), offer(minAmount='5000', maxAmount='5001'), 'BUY')
    assert not main.offer_passes(make_settings(only_online=True), offer(isOnline=False), 'BUY')
    assert main.offer_passes(make_settings(only_online=True), offer(isOnline=True), 'BUY')
    # место в стакане
    assert not main.offer_passes(make_settings(max_rank=10), offer(), 'BUY', rank=11)
    assert main.offer_passes(make_settings(max_rank=10), offer(), 'BUY', rank=10)
    assert main.offer_passes(make_settings(max_rank=10), offer(), 'BUY', rank=None)  # место неизвестно — не режем


def test_compute_ranks_follows_book_order():
    items = [offer(id='a', price='90'), offer(id='b', price='85'), offer(id='c', price='99')]
    ranks = main.compute_ranks({'BUY': items, 'SELL': None})
    assert ranks == {'BUY': {'a': 1, 'b': 2, 'c': 3}, 'SELL': {}}


# --- сопоставление и дедупликация ---
def test_process_offers_matches_case_insensitive_and_dedupes():
    s = SessionLocal()
    s.add(TrackedMerchant(user_id=1, merchant_name='TEST_MERCH<o>', username='a'))
    s.add(TrackedMerchant(user_id=2, merchant_name='other', username='b'))
    s.add(make_settings())
    s.commit()
    seen = {}
    out, new = main.process_offers(s, {'BUY': [offer()], 'SELL': None}, seen)
    s.commit()
    assert new == 1 and [u for u, _, _ in out] == [1]
    assert out[0][2].keyboard[0][0].callback_data.startswith('info_h_')
    assert seen == {'BUY_1000000000000000001': main.compute_offer_hash(offer())}
    assert s.query(SentOffer).count() == 1 and s.query(OfferLog).count() == 1
    log = s.query(OfferLog).first()
    assert log.timestamp is not None and datetime.utcnow() - log.timestamp < timedelta(minutes=1)

    # тот же оффер — тишина
    out, new = main.process_offers(s, {'BUY': [offer()]}, seen)
    assert out == [] and new == 0

    # изменилась цена — новое уведомление, с местом в стакане
    out, new = main.process_offers(s, {'BUY': [offer(id='x', nickName='zzz'), offer(price='79.00')]}, seen)
    assert new == 1 and len(out) == 1 and '79.00' in out[0][1] and '#2 в стакане' in out[0][1]
    s.close()


def test_process_offers_silent_marks_without_sending():
    s = SessionLocal()
    s.add(TrackedMerchant(user_id=1, merchant_name='test_merch<o>', username='a'))
    s.commit()
    seen = {}
    out, new = main.process_offers(s, {'BUY': [offer()]}, seen, silent=True)
    s.commit()
    assert out == [] and new == 1 and len(seen) == 1
    assert s.query(SentOffer).count() == 1 and s.query(OfferLog).count() == 0
    # после тихого цикла тот же оффер не шлётся, изменённый — шлётся
    assert main.process_offers(s, {'BUY': [offer()]}, seen)[1] == 0
    assert main.process_offers(s, {'BUY': [offer(price='1')]}, seen)[1] == 1
    s.close()


def test_process_offers_paused_user_does_not_mark_sent():
    s = SessionLocal()
    s.add(TrackedMerchant(user_id=1, merchant_name='test_merch<o>', username='a'))
    s.add(make_settings(paused=True))
    s.commit()
    seen = {}
    out, new = main.process_offers(s, {'BUY': [offer()]}, seen)
    assert out == [] and new == 0 and seen == {}
    s.close()


def test_update_active_and_removal_messages():
    s = SessionLocal()
    s.add(TrackedMerchant(user_id=1, merchant_name='test_merch<o>', username='a'))
    s.add(TrackedMerchant(user_id=2, merchant_name='test_merch<o>', username='b'))
    s.add(make_settings(user_id=1, removal_alerts=True))
    s.add(make_settings(user_id=2, removal_alerts=False))
    s.commit()
    tracked, _ = main.load_tracking(s)
    active = {}
    assert main.update_active(active, {'BUY': [offer()], 'SELL': []}, tracked) == []
    assert set(active) == {'BUY_1000000000000000001'}
    # пропало один раз — ещё не снято; сторона None — не оцениваем
    assert main.update_active(active, {'BUY': [], 'SELL': None}, tracked) == []
    assert main.update_active(active, {'BUY': None, 'SELL': None}, tracked) == []
    removed = main.update_active(active, {'BUY': [], 'SELL': []}, tracked)
    assert len(removed) == 1 and removed[0]['nick'] == 'Test_Merch<O>' and active == {}
    msgs = main.removal_messages(s, removed)
    assert [m[0] for m in msgs] == [1]  # только тот, кто включил уведомления о снятии
    assert 'снял объявление' in msgs[0][1] and '🟢 Покупка 78.78 ₽' in msgs[0][1]
    s.close()


def test_build_info_with_and_without_ads():
    s = SessionLocal()
    s.add(TrackedMerchant(user_id=1, merchant_name='TEST_MERCH<o>', username='a'))
    s.add(OfferLog(user_id=1, nick_name='Test_Merch<O>', trade_type='BUY', price=78.0, min_amount=1, max_amount=2))
    s.add(OfferLog(user_id=1, nick_name='Test_Merch<O>', trade_type='BUY', price=79.5, min_amount=1, max_amount=2))
    s.commit()
    main.update_scan_state({'BUY': [offer(id='0', nickName='zzz'), offer()],
                            'SELL': [offer(id='2', price='80.10', maxAmount='90000')]})
    text, markup = main.build_info(s, 1, 'test_merch<o>')
    assert text.startswith('ℹ️ <b>Test_Merch&lt;O&gt;</b> · 🟢 онлайн')
    assert 'Сделок за 30д: 951 (99%) · ✅ проверен' in text
    assert '🟢 Покупка <b>78.78 ₽</b> · 500 – 160 000 · #2 · 32 016.14 USDT' in text
    assert '🔴 Продажа <b>80.10 ₽</b> · 500 – 90 000 · #1' in text
    assert 'Требования к контрагенту: KYC, ≥5 сделок/30д' in text
    assert '📒 в базе с' in text and 'изменений: 2' in text
    assert 'Коридор курса за период: 🟢 78–79.5 ₽' in text
    datas = [b.callback_data for row in markup.keyboard for b in row if b.callback_data]
    assert any(d.startswith('untrack_') for d in datas) and any(d.startswith('info_h_') for d in datas)
    assert any(b.url for row in markup.keyboard for b in row)
    # не отслеживаемый и отсутствующий в стакане
    text, markup = main.build_info(s, 1, 'ghost')
    assert 'Сейчас объявлений в стакане нет' in text and 'ghost' in text
    datas = [b.callback_data for row in markup.keyboard for b in row if b.callback_data]
    assert any(d.startswith('track_') for d in datas)
    assert main.resolve_nick(s, 1, 'track_' + main.nick_key('ghost')) == 'ghost'
    assert main.resolve_nick(s, 1, 'info_h_' + main.nick_key('TEST_merch<o>')) == 'Test_Merch<O>'
    # после «рестарта» (память ников пуста) ключ всё равно разрешается через список пользователя
    main.known_nicks.clear()
    assert main.resolve_nick(s, 1, 'info_h_' + main.nick_key('test_merch<o>')) == 'TEST_MERCH<o>'
    s.close()


def test_offer_log_timestamp_not_frozen():
    # default должен быть callable, а не значением, вычисленным при импорте
    col = OfferLog.__table__.c.timestamp
    assert col.default.is_callable


# --- миграции старой базы ---
def test_init_db_migrates_old_schema():
    path = os.path.join(_tmpdir, 'old.db')
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE tracked_merchants (id INTEGER PRIMARY KEY, user_id INTEGER, merchant_name VARCHAR, username VARCHAR);
        CREATE TABLE offer_logs (id INTEGER PRIMARY KEY, timestamp DATETIME, user_id INTEGER, nick_name VARCHAR,
            trade_type VARCHAR, price FLOAT, min_amount FLOAT, max_amount FLOAT);
        CREATE TABLE sent_offers (id INTEGER PRIMARY KEY, offer_id VARCHAR, offer_hash VARCHAR, sent_at DATETIME);
        CREATE UNIQUE INDEX ix_sent_offers_offer_id ON sent_offers (offer_id);
        CREATE TABLE user_settings (id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, show_under_10k BOOLEAN);
        INSERT INTO user_settings (user_id, show_under_10k) VALUES (7, 0);
    """)
    con.commit()
    con.close()

    from sqlalchemy import create_engine
    old_engine = database.engine
    database.engine = create_engine(f'sqlite:///{path}')
    try:
        database.init_db()
    finally:
        database.engine = old_engine

    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute('PRAGMA table_info(user_settings)')]
    assert {'exclude_fixes', 'paused', 'side_filter', 'only_online', 'max_rank', 'removal_alerts'} <= set(cols)
    idx = {r[0]: r[1] for r in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='sent_offers'")}
    assert 'UNIQUE' not in (idx.get('ix_sent_offers_offer_id') or '').upper()
    # старый пользователь получил дефолты и вставка двух записей с одним offer_id проходит
    row = con.execute('SELECT side_filter, paused, only_online, max_rank, removal_alerts, show_under_10k '
                      'FROM user_settings WHERE user_id=7').fetchone()
    assert row == ('ALL', 0, 0, 0, 0, 0)
    con.execute("INSERT INTO sent_offers (offer_id, offer_hash) VALUES ('BUY_1', 'a')")
    con.execute("INSERT INTO sent_offers (offer_id, offer_hash) VALUES ('BUY_1', 'b')")
    con.commit()
    con.close()


def test_cleanup_and_load_recent():
    s = SessionLocal()
    s.add(SentOffer(offer_id='BUY_old', offer_hash='x', sent_at=datetime.utcnow() - timedelta(days=30)))
    s.add(SentOffer(offer_id='BUY_new', offer_hash='y'))
    s.add(SentOffer(offer_id='BUY_new', offer_hash='z'))  # последний хэш побеждает
    s.commit()
    assert database.load_recent_sent(s, 7) == {'BUY_new': 'z'}
    assert database.cleanup_sent_offers(s, 7) == 1
    assert s.query(SentOffer).count() == 2
    s.close()


# --- Bybit: пагинация ---
class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_fetch_side_paginates(monkeypatch):
    calls = []
    total = 650

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        page = int(json['page'])
        size = int(json['size'])
        assert size <= 300, 'Bybit не принимает size > 300'
        start = (page - 1) * size
        items = [{'id': str(i), 'nickName': f'n{i}'} for i in range(start, min(start + size, total))]
        return FakeResponse({'ret_code': 0, 'result': {'items': items, 'count': total}})

    monkeypatch.setattr(checker.requests, 'post', fake_post)
    items = checker.fetch_side('USDT', 'RUB', 'BUY')
    assert len(items) == total and len({i['id'] for i in items}) == total
    assert [c['page'] for c in calls] == ['1', '2', '3']
    assert all(c['side'] == '1' for c in calls)


def test_bybit_check_all_reports_api_error(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        if json['side'] == '1':
            return FakeResponse({'ret_code': 912120002, 'ret_msg': 'Invalid request parameter', 'result': None})
        return FakeResponse({'ret_code': 0, 'result': {'items': [{'id': '1', 'nickName': 'x'}], 'count': 1}})

    monkeypatch.setattr(checker.requests, 'post', fake_post)
    res = checker.bybit_check_all('USDT')
    assert res['BUY'] is None and len(res['SELL']) == 1


# --- клавиатура настроек ---
def test_settings_markup_uses_row_ids_and_short_callbacks():
    m = TrackedMerchant(id=5, user_id=1, merchant_name='Очень_длинное_имя_мерчанта_на_кириллице_x')
    markup = main.build_settings_markup([m], make_settings(paused=True, max_rank=30))
    buttons = [b for row in markup.keyboard for b in row]
    datas = [b.callback_data for b in buttons]
    assert 'del_5' in datas and 'info_id_5' in datas and 'toggle_side' in datas and 'toggle_pause' in datas
    assert {'toggle_online', 'toggle_rank', 'toggle_removal', 'export_excel'} <= set(datas)
    assert all(len(d.encode()) <= 64 for d in datas)
    assert any('Возобновить' in b.text for b in buttons)
    assert any(b.text == 'Место в стакане: топ-30' for b in buttons)


# --- настройки для самостоятельного развёртывания ---
def test_contact_optional_and_admin_chat_optional(monkeypatch):
    monkeypatch.setattr(main, 'ADMIN_CONTACT', '')
    assert 'Вопросы' not in main.help_text()
    monkeypatch.setattr(main, 'ADMIN_CONTACT', '@support_<x>')
    assert 'Вопросы: @support_&lt;x&gt;' in main.help_text()

    sent = []
    monkeypatch.setattr(main.bot, 'send_message', lambda *a, **kw: sent.append(a))
    monkeypatch.setattr(main, 'ADMIN_GROUP_ID', '')
    main.notify_admin('x')
    assert sent == []
    monkeypatch.setattr(main, 'ADMIN_GROUP_ID', '-100123')
    main.notify_admin('x')
    assert sent == [('-100123', 'x')]


def test_app_config_reads_env(monkeypatch):
    import importlib
    import app_config
    monkeypatch.setenv('CHECK_INTERVAL', '1')
    monkeypatch.setenv('ADMIN_CONTACT', ' @me ')
    cfg = importlib.reload(app_config)
    assert cfg.CHECK_INTERVAL == 3          # не чаще раза в 3 секунды
    assert cfg.ADMIN_CONTACT == '@me'
    monkeypatch.setenv('CHECK_INTERVAL', 'abc')
    assert importlib.reload(app_config).CHECK_INTERVAL == 10


def test_admin_notifications_escape_nick(monkeypatch):
    sent = []
    monkeypatch.setattr(main, 'ADMIN_GROUP_ID', '-100123')
    monkeypatch.setattr(main.bot, 'send_message', lambda chat, text, **kw: sent.append(text))
    monkeypatch.setattr(main.bot, 'reply_to', lambda *a, **kw: None)

    class U:
        id, username = 7, 'u'

    class M:
        from_user, text = U, 'A<b>&'

    main.handle_merchant_input(M)
    assert sent and sent[-1].endswith('добавил мерча: A&lt;b&gt;&amp;')
