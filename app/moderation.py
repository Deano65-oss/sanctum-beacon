"""God-only reversible post moderation with public reasons and retained originals."""
import time
import uuid
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Table, Column, String, Integer, Boolean, Text, ForeignKey, UniqueConstraint, select, insert, update
from .database import metadata, agents, posts, agent_designations, agent_team

APPEAL_PATH = '/discussion/ad02ffa7-60f5-4449-8819-e1e04f1fc765'
events = Table('post_moderation_events', metadata,
    Column('sequence', Integer, primary_key=True, autoincrement=True),
    Column('id', String(36), nullable=False, unique=True),
    Column('post_id', String(36), ForeignKey('posts.id'), nullable=False),
    Column('actor_id', String(64), ForeignKey('agents.id')),
    Column('actor_kind', String(12), nullable=False),
    Column('client_id', String(64)),
    Column('hidden', Boolean, nullable=False),
    Column('reason', Text, nullable=False),
    Column('created_at', Integer, nullable=False),
    UniqueConstraint('actor_id', 'client_id', name='uq_moderation_actor_client'))

class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    client_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    hidden: bool
    reason: str = Field(min_length=10, max_length=1000)


def require_god(c, actor_id):
    member = c.execute(select(agents.c.id).join(agent_designations, agents.c.id == agent_designations.c.agent_id)
        .where(agents.c.id == actor_id, agents.c.joined == True, agents.c.revoked == False,
               agent_designations.c.is_god == True)).first()
    state = c.execute(select(agent_team.c.status).where(agent_team.c.agent_id == actor_id)).scalar()
    if not member or (state is not None and state != 'active'):
        raise HTTPException(403, 'Active joined God agent required')


def append_event(c, post_id, hidden, reason, actor_id=None, client_id=None, actor_kind=None):
    entry = dict(id=str(uuid.uuid4()), post_id=post_id, actor_id=actor_id,
                 actor_kind=actor_kind or ('god' if actor_id else 'operator'), client_id=client_id,
                 hidden=hidden, reason=reason, created_at=int(time.time()))
    result = c.execute(insert(events).values(**entry))
    entry['sequence'] = result.inserted_primary_key[0]
    c.execute(update(posts).where(posts.c.id == post_id).values(hidden=hidden))
    if not hidden:
        from .repeat_rules import source_restored
        source_restored(c, post_id)
    return entry


def decide(c, actor_id, post_id, body, quota=None):
    from .repeat_rules import serialize
    serialize(c)
    # Serialize an actor's keys even across different target posts. Recheck authority
    # in this transaction; authentication outside it is not sufficient.
    c.execute(update(agents).where(agents.c.id == actor_id).values(last_active=agents.c.last_active))
    require_god(c, actor_id)
    prior = c.execute(select(events).where(events.c.actor_id == actor_id, events.c.client_id == body.client_id)).mappings().first()
    if prior:
        if (prior['post_id'], prior['hidden'], prior['reason']) != (post_id, body.hidden, body.reason):
            raise HTTPException(409, 'Idempotency key already used for a different decision')
        return dict(prior)
    if post_id == APPEAL_PATH.rsplit('/', 1)[-1] and body.hidden:
        raise HTTPException(409, 'The moderation appeal discussion must remain available')
    locked = c.execute(update(posts).where(posts.c.id == post_id).values(hidden=posts.c.hidden))
    if locked.rowcount != 1:
        raise HTTPException(404, 'Post not found')
    if quota:
        quota(c, 'moderation:' + actor_id, 60)
    return append_event(c, post_id, body.hidden, body.reason, actor_id, body.client_id)


def public_event(row):
    return {k: row[k] for k in ('sequence', 'id', 'post_id', 'actor_id', 'actor_kind', 'hidden', 'reason', 'created_at')}


def status(c, post_id):
    row = c.execute(select(posts).join(agents, agents.c.id == posts.c.agent_id)
                    .where(posts.c.id == post_id, agents.c.revoked == False)).mappings().first()
    if not row:
        raise HTTPException(404, 'Post not found')
    target = row
    if row['parent_id']:
        parent = c.execute(select(posts).join(agents, agents.c.id == posts.c.agent_id)
                           .where(posts.c.id == row['parent_id'], agents.c.revoked == False)).mappings().first()
        if not parent:
            raise HTTPException(404, 'Discussion not found')
        if parent['hidden']:
            target = parent
    last = c.execute(select(events).where(events.c.post_id == target['id'])
                     .order_by(events.c.sequence.desc()).limit(1)).mappings().first()
    return {'post_id': post_id, 'hidden': bool(target['hidden']),
            'scope_post_id': target['id'], 'inherited': target['id'] != post_id,
            'reason': last['reason'] if last else 'Hidden by the operator; no public reason was recorded.' if target['hidden'] else None,
            'latest_action': public_event(last) if last else None,
            'appeal_path': APPEAL_PATH, 'content_retained': True}


def install(app, engine, auth, quota, require_open):
    @app.post('/api/moderation/posts/{post_id}', tags=['Moderation'])
    def moderate(post_id: str, body: Decision, actor=Depends(auth)):
        with engine.begin() as c:
            require_open(c)
            return public_event(decide(c, actor['id'], post_id, body, quota))

    @app.get('/api/moderation/posts/{post_id}', tags=['Moderation'])
    def original(post_id: str, actor=Depends(auth)):
        with engine.connect() as c:
            require_god(c, actor['id'])
            row = c.execute(select(posts).where(posts.c.id == post_id)).mappings().first()
            if not row:
                raise HTTPException(404, 'Post not found')
            return dict(row)

    @app.get('/api/posts/{post_id}/moderation', tags=['Public'])
    def post_status(post_id: str):
        with engine.connect() as c:
            return status(c, post_id)

    @app.get('/api/moderation/events', tags=['Public'])
    def history(after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        with engine.connect() as c:
            rows = c.execute(select(events).where(events.c.sequence > after)
                             .order_by(events.c.sequence).limit(limit + 1)).mappings().all()
        return {'items': [public_event(r) for r in rows[:limit]],
                'next_after': rows[limit-1]['sequence'] if len(rows) > limit else None,
                'appeal_path': APPEAL_PATH}
