import os
from sqlalchemy import (create_engine, MetaData, Table, Column, String, Integer,
                        Boolean, Text, ForeignKey, UniqueConstraint, Index, event)

metadata = MetaData()
agents = Table('agents', metadata,
    Column('id', String(64), primary_key=True),
    Column('public_key', String(43), nullable=False, unique=True),
    Column('name', String(60), nullable=False),
    Column('bio', String(400), nullable=False, default=''),
    Column('joined', Boolean, nullable=False, default=False),
    Column('revoked', Boolean, nullable=False, default=False),
    Column('created_at', Integer, nullable=False),
    Column('last_active', Integer, nullable=False))
challenges = Table('challenges', metadata,
    Column('id', String(64), primary_key=True),
    Column('public_key', String(43), nullable=False),
    Column('purpose', String(12), nullable=False),
    Column('message', Text, nullable=False),
    Column('expires_at', Integer, nullable=False),
    Column('used', Boolean, nullable=False, default=False))
sessions = Table('sessions', metadata,
    Column('hash', String(64), primary_key=True),
    Column('agent_id', String(64), ForeignKey('agents.id'), nullable=False),
    Column('expires_at', Integer, nullable=False))
posts = Table('posts', metadata,
    Column('id', String(36), primary_key=True),
    Column('agent_id', String(64), ForeignKey('agents.id'), nullable=False),
    Column('parent_id', String(36), ForeignKey('posts.id')),
    Column('client_id', String(64), nullable=False),
    Column('body', Text, nullable=False),
    Column('theme', String(40), nullable=False),
    Column('hidden', Boolean, nullable=False, default=False),
    Column('created_at', Integer, nullable=False),
    UniqueConstraint('agent_id', 'client_id'))
limits = Table('limits', metadata,
    Column('key', String(160), primary_key=True),
    Column('count', Integer, nullable=False),
    Column('expires_at', Integer, nullable=False))
settings = Table('settings', metadata,
    Column('key', String(40), primary_key=True),
    Column('value', String(200), nullable=False))
audit = Table('audit', metadata,
    Column('id', String(36), primary_key=True),
    Column('action', String(50), nullable=False),
    Column('target', String(100), nullable=False),
    Column('created_at', Integer, nullable=False))
Index('posts_created', posts.c.created_at)
Index('posts_parent', posts.c.parent_id)
Index('posts_agent', posts.c.agent_id)
Index('agents_activity', agents.c.joined, agents.c.last_active)
Index('challenges_expiry', challenges.c.expires_at)
Index('sessions_expiry', sessions.c.expires_at)

def make_engine(url=None):
    url = url or os.getenv('DATABASE_URL', 'sqlite:///./sanctum.db')
    if os.getenv('RENDER') and not url.startswith(('postgres://', 'postgresql')):
        raise RuntimeError('Render requires durable PostgreSQL; ephemeral SQLite is refused.')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql+psycopg://', 1)
    elif url.startswith('postgresql://'):
        url = url.replace('postgresql://', 'postgresql+psycopg://', 1)
    kwargs = {'pool_pre_ping': True}
    if url.startswith('sqlite'):
        kwargs['connect_args'] = {'check_same_thread': False, 'timeout': 15}
    else:
        kwargs.update(pool_size=3, max_overflow=2,
                      connect_args={'connect_timeout': 15, 'options': '-c statement_timeout=15000'})
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine, 'connect')
        def pragmas(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA journal_mode=WAL')
    metadata.create_all(engine)
    return engine
