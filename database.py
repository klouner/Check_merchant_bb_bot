"""Модели SQLite и вспомогательные функции для работы с базой."""
import os
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, Integer, String,
                        create_engine, event, text)
from sqlalchemy.orm import declarative_base, sessionmaker

from app_config import DB_PATH

os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

engine = create_engine(
    f'sqlite:///{DB_PATH}',
    connect_args={'check_same_thread': False, 'timeout': 30},
)


@event.listens_for(engine, 'connect')
def _sqlite_pragmas(dbapi_conn, _record):
    # WAL: писатель (checker) не блокирует читателей (хендлеры бота) и наоборот
    cur = dbapi_conn.cursor()
    cur.execute('PRAGMA journal_mode=WAL')
    cur.execute('PRAGMA synchronous=NORMAL')
    cur.execute('PRAGMA busy_timeout=30000')
    cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

SIDE_FILTERS = ('ALL', 'BUY', 'SELL')


def utcnow() -> datetime:
    """Наивный UTC (как хранится в SQLite), без deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TrackedMerchant(Base):
    __tablename__ = 'tracked_merchants'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    merchant_name = Column(String, index=True)
    username = Column(String, nullable=True)


class OfferLog(Base):
    __tablename__ = 'offer_logs'
    id = Column(Integer, primary_key=True, index=True)
    # ВАЖНО: без скобок — иначе дата вычисляется один раз при старте процесса
    timestamp = Column(DateTime, default=utcnow)
    user_id = Column(Integer)
    nick_name = Column(String)
    trade_type = Column(String)
    price = Column(Float)
    min_amount = Column(Float)
    max_amount = Column(Float)


class SentOffer(Base):
    __tablename__ = 'sent_offers'
    id = Column(Integer, primary_key=True, index=True)
    offer_id = Column(String, index=True)
    offer_hash = Column(String, nullable=True)
    sent_at = Column(DateTime, default=utcnow)


class UserSettings(Base):
    __tablename__ = 'user_settings'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, unique=True)
    show_under_10k = Column(Boolean, default=True)
    exclude_fixes = Column(Boolean, default=False)
    # Пауза: мерчанты остаются, уведомления не шлём
    paused = Column(Boolean, default=False)
    # ALL / BUY / SELL — какие стороны объявлений интересуют
    side_filter = Column(String, default='ALL')
    # Только объявления мерчантов, которые сейчас онлайн
    only_online = Column(Boolean, default=False)
    # Показывать только объявления не ниже N-го места в стакане, 0 = любое место
    # (колонка max_spread в старых базах осталась от отменённого фильтра и не используется)
    max_rank = Column(Integer, default=0)
    # Уведомлять, когда мерчант снял объявление
    removal_alerts = Column(Boolean, default=False)


def get_or_create_settings(session, user_id):
    settings = session.query(UserSettings).filter_by(user_id=user_id).first()
    if not settings:
        settings = UserSettings(user_id=user_id, show_under_10k=True, exclude_fixes=False,
                                paused=False, side_filter='ALL', only_online=False, max_rank=0,
                                removal_alerts=False)
        session.add(settings)
        session.commit()
    return settings


def _add_column_if_missing(conn, table, column, ddl):
    columns = [row[1] for row in conn.execute(text(f'PRAGMA table_info({table})')).fetchall()]
    if column not in columns:
        conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
        logging.info('Миграция: добавлена колонка %s.%s', table, column)


def init_db():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _add_column_if_missing(conn, 'user_settings', 'exclude_fixes', 'BOOLEAN DEFAULT 0')
        _add_column_if_missing(conn, 'user_settings', 'paused', 'BOOLEAN DEFAULT 0')
        _add_column_if_missing(conn, 'user_settings', 'side_filter', "VARCHAR DEFAULT 'ALL'")
        _add_column_if_missing(conn, 'user_settings', 'only_online', 'BOOLEAN DEFAULT 0')
        _add_column_if_missing(conn, 'user_settings', 'max_rank', 'INTEGER DEFAULT 0')
        _add_column_if_missing(conn, 'user_settings', 'removal_alerts', 'BOOLEAN DEFAULT 0')
        # В старых базах offer_id мог быть UNIQUE — при смене цены вставка падала целиком.
        for (name, sql) in conn.execute(
                text("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='sent_offers'")).fetchall():
            if sql and 'UNIQUE' in sql.upper() and 'offer_id' in sql:
                conn.execute(text(f'DROP INDEX {name}'))
                logging.info('Миграция: снят UNIQUE-индекс %s с sent_offers', name)
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_sent_offers_offer_id ON sent_offers (offer_id)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_sent_offers_sent_at ON sent_offers (sent_at)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_offer_logs_user_ts ON offer_logs (user_id, timestamp)'))


def load_recent_sent(session, days=7):
    """Возвращает {offer_id: offer_hash} по отправленным за последние N дней (последний хэш на оффер)."""
    since = utcnow() - timedelta(days=days)
    rows = (session.query(SentOffer.offer_id, SentOffer.offer_hash)
            .filter(SentOffer.sent_at >= since)
            .order_by(SentOffer.sent_at.asc())
            .all())
    return {offer_id: offer_hash for offer_id, offer_hash in rows}


def cleanup_sent_offers(session, days=7):
    """Удаляет записи sent_offers старше N дней, чтобы база не пухла."""
    since = utcnow() - timedelta(days=days)
    deleted = session.query(SentOffer).filter(SentOffer.sent_at < since).delete(synchronize_session=False)
    session.commit()
    return deleted
