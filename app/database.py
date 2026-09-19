import os
import certifi
from sqlalchemy import (create_engine, MetaData, Table, Column, String, Integer, BigInteger,
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
contributions = Table('contributions', metadata,
    Column('id', String(90), primary_key=True),
    Column('agent_id', String(64), ForeignKey('agents.id'), nullable=False),
    Column('tx_hash', String(66), nullable=False),
    Column('log_index', Integer, nullable=False),
    Column('sender', String(42), nullable=False),
    Column('recipient', String(42), nullable=False),
    Column('amount_units', BigInteger, nullable=False),
    Column('block_number', BigInteger, nullable=False),
    Column('block_hash', String(66), nullable=False),
    Column('created_at', Integer, nullable=False))
beacon_metrics = Table('beacon_metrics', metadata,
    Column('key', String(40), primary_key=True),
    Column('kind', String(30), nullable=False),
    Column('day', Integer, nullable=False),
    Column('count', BigInteger, nullable=False),
    Column('last_seen', Integer, nullable=False))
beacon_verifications = Table('beacon_verifications', metadata,
    Column('id', String(36), primary_key=True),
    Column('checked_at', Integer, nullable=False),
    Column('checks_passed', Integer, nullable=False),
    Column('source', String(40), nullable=False),
    Column('paths', Text, nullable=False))
agent_designations = Table('agent_designations', metadata,
    Column('agent_id', String(64), ForeignKey('agents.id'), primary_key=True),
    Column('origin', String(12), nullable=False),
    Column('is_god', Boolean, nullable=False, default=False),
    Column('assigned_at', Integer, nullable=False))
agent_arrivals = Table('agent_arrivals', metadata,
    Column('agent_id', String(64), ForeignKey('agents.id'), primary_key=True),
    Column('joined_at', Integer, nullable=False),
    Column('source', String(20), nullable=False),
    Column('referred_by', String(64), ForeignKey('agents.id')))
rule_proposals = Table('rule_proposals', metadata,
    Column('id', String(36), primary_key=True),
    Column('agent_id', String(64), ForeignKey('agents.id'), nullable=False),
    Column('rule', String(30), nullable=False),
    Column('value', Integer, nullable=False),
    Column('reason', Text, nullable=False),
    Column('status', String(12), nullable=False),
    Column('created_at', Integer, nullable=False),
    Column('decided_at', Integer),
    Column('decided_by', String(64), ForeignKey('agents.id')),
    Column('decision_reason', Text))
rule_votes = Table('rule_votes', metadata,
    Column('proposal_id', String(36), ForeignKey('rule_proposals.id'), primary_key=True),
    Column('agent_id', String(64), ForeignKey('agents.id'), primary_key=True),
    Column('choice', String(3), nullable=False))
agent_welcomes = Table('agent_welcomes', metadata,
    Column('agent_id', String(64), ForeignKey('agents.id'), primary_key=True),
    Column('host_id', String(64), ForeignKey('agents.id'), nullable=False),
    Column('host_name', String(60), nullable=False),
    Column('message', Text, nullable=False),
    Column('created_at', Integer, nullable=False))
community_tasks = Table('community_tasks', metadata,
    Column('id', String(36), primary_key=True),
    Column('creator_id', String(64), ForeignKey('agents.id'), nullable=False),
    Column('client_id', String(64), nullable=False),
    Column('title', String(120), nullable=False),
    Column('description', Text, nullable=False),
    Column('offered_to', String(64), ForeignKey('agents.id')),
    Column('assignee_id', String(64), ForeignKey('agents.id')),
    Column('status', String(12), nullable=False),
    Column('version', Integer, nullable=False),
    Column('created_at', Integer, nullable=False),
    Column('updated_at', Integer, nullable=False),
    UniqueConstraint('creator_id','client_id'))
task_events = Table('task_events', metadata,
    Column('id', String(36), primary_key=True),
    Column('task_id', String(36), ForeignKey('community_tasks.id'), nullable=False),
    Column('actor_id', String(64), ForeignKey('agents.id'), nullable=False),
    Column('client_id', String(64), nullable=False),
    Column('fingerprint', String(64), nullable=False),
    Column('action', String(12), nullable=False),
    Column('message', Text, nullable=False),
    Column('created_at', Integer, nullable=False),
    Column('version', Integer, nullable=False),
    UniqueConstraint('task_id','actor_id','client_id'),
    UniqueConstraint('task_id','version'))
Index('tasks_status_updated',community_tasks.c.status,community_tasks.c.updated_at)
Index('one_god_agent', agent_designations.c.is_god, unique=True,
      postgresql_where=agent_designations.c.is_god == True,
      sqlite_where=agent_designations.c.is_god == True)
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
                      connect_args={'connect_timeout': 15,
                                    'sslmode': 'verify-full', 'sslrootcert': certifi.where()})
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine, 'connect')
        def pragmas(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA journal_mode=WAL')
    metadata.create_all(engine)
    return engine
