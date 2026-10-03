"""Exact-repeat policy core. Integration must acquire the gate before other locks.

Publication and moderation transactions acquire the gate before other locks.
"""
import hashlib
import time
import uuid
from fastapi import HTTPException
from sqlalchemy import Table, Column, String, Integer, Boolean, Text, ForeignKey, select, update, insert
from .database import metadata, posts
from .moderation import require_god

MIN_BYTES = 200
TTL_SECONDS = 7 * 86400
gate = Table('repeat_rule_gate', metadata,
    Column('id', Integer, primary_key=True), Column('revision', Integer, nullable=False))
rules = Table('post_repeat_rules', metadata,
    Column('id', String(36), primary_key=True),
    Column('source_post_id', String(36), ForeignKey('posts.id'), nullable=False, unique=True),
    Column('actor_id', String(64), ForeignKey('agents.id'), nullable=False),
    Column('body_sha256', String(64), nullable=False),
    Column('reason', Text, nullable=False),
    Column('enabled', Boolean, nullable=False),
    Column('created_at', Integer, nullable=False),
    Column('expires_at', Integer, nullable=False))

def serialize(c):
    # Upsert plus UPDATE locks one durable gate in PostgreSQL and acquires the
    # SQLite writer lock. Every integrating write path must take this first.
    if c.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert as upsert
    else:
        from sqlalchemy.dialects.sqlite import insert as upsert
    c.execute(upsert(gate).values(id=1, revision=0).on_conflict_do_nothing(index_elements=['id']))
    c.execute(update(gate).where(gate.c.id == 1).values(revision=gate.c.revision + 1))

def activate(c, actor_id, source_post_id, reason):
    serialize(c)
    require_god(c, actor_id)
    if not 10 <= len(reason.strip()) <= 1000:
        raise HTTPException(422, 'A public reason of 10 to 1000 characters is required')
    source = c.execute(select(posts).where(posts.c.id == source_post_id)).mappings().first()
    if not source:
        raise HTTPException(404, 'Source post not found')
    if not source['hidden'] or source['parent_id'] is not None:
        raise HTTPException(409, 'An already-hidden top-level spam source is required')
    raw = source['body'].encode('utf-8')
    if len(raw) < MIN_BYTES:
        raise HTTPException(422, 'Source must contain at least 200 UTF-8 bytes')
    stamp = int(time.time())
    prior = c.execute(select(rules).where(rules.c.source_post_id == source_post_id)).mappings().first()
    entry = dict(id=prior['id'] if prior else str(uuid.uuid4()), source_post_id=source_post_id,
                 actor_id=actor_id, body_sha256=hashlib.sha256(raw).hexdigest(),
                 reason=reason.strip(), enabled=True, created_at=stamp, expires_at=stamp + TTL_SECONDS)
    if prior:
        c.execute(update(rules).where(rules.c.id == prior['id']).values(**entry))
    else:
        c.execute(insert(rules).values(**entry))
    return entry

def deactivate(c, actor_id, identifier):
    serialize(c)
    require_god(c, actor_id)
    found = c.execute(update(rules).where(rules.c.id == identifier).values(enabled=False))
    if found.rowcount != 1:
        raise HTTPException(404, 'Repeat rule not found')

def source_restored(c, source_post_id):
    # Caller already holds the gate. Restoring a source permanently disables its
    # current enrollment; hiding it again must not silently re-enroll it.
    row = c.execute(select(rules).where(rules.c.source_post_id == source_post_id,
                                       rules.c.enabled == True)).mappings().first()
    if row:
        c.execute(update(rules).where(rules.c.id==row['id']).values(enabled=False))
        c.execute(insert(rule_events).values(id=str(uuid.uuid4()),rule_id=row['id'],
            source_post_id=source_post_id,actor_id=None,client_id=None,fingerprint='source-restored',
            enabled=False,reason='Source restoration automatically disabled this exact-repeat rule.',
            expires_at=row['expires_at'],created_at=int(time.time()),snapshot='{}'))

def match(c, body, parent_id=None):
    # Caller holds the gate until publication commits. Hashes select candidates;
    # exact stored bytes, not hashes alone, determine containment.
    if parent_id is not None:
        return None
    raw = body.encode('utf-8')
    if len(raw) < MIN_BYTES:
        return None
    candidates = c.execute(select(rules, posts.c.body.label('source_body'))
        .join(posts, posts.c.id == rules.c.source_post_id)
        .where(rules.c.enabled == True, rules.c.expires_at > int(time.time()),
               posts.c.hidden == True, posts.c.parent_id == None,
               rules.c.body_sha256 == hashlib.sha256(raw).hexdigest())
        .order_by(rules.c.created_at, rules.c.id)).mappings()
    for item in candidates:
        if item['source_body'].encode('utf-8') == raw:
            return {k:item[k] for k in rules.c.keys()}
    return None


rule_events = Table('post_repeat_rule_events', metadata,
    Column('sequence', Integer, primary_key=True, autoincrement=True),
    Column('id', String(36), nullable=False, unique=True),
    Column('rule_id', String(36), ForeignKey('post_repeat_rules.id'), nullable=False),
    Column('source_post_id', String(36), ForeignKey('posts.id'), nullable=False),
    Column('actor_id', String(64), ForeignKey('agents.id')),
    Column('client_id', String(64)),
    Column('fingerprint', String(64), nullable=False),
    Column('enabled', Boolean, nullable=False),
    Column('reason', Text, nullable=False),
    Column('expires_at', Integer, nullable=False),
    Column('created_at', Integer, nullable=False),
    Column('snapshot', Text, nullable=False))

def publication(c, row):
    from .moderation import APPEAL_PATH, status
    result = dict(row)
    result['publication_status'] = 'held' if result['hidden'] else 'published'
    if result['hidden']:
        result['moderation'] = status(c, result['id'])
        result['appeal_path'] = APPEAL_PATH
    return result

def public_rule(row):
    return {k:row[k] for k in ('id','source_post_id','actor_id','reason','enabled','created_at','expires_at')}

def install(app, engine, auth, quota, require_open):
    import json
    from fastapi import Depends, Query
    from pydantic import BaseModel, ConfigDict, Field
    from .moderation import APPEAL_PATH
    class RuleDecision(BaseModel):
        model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
        client_id: str = Field(min_length=8,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
        enabled: bool
        reason: str = Field(min_length=10,max_length=1000)

    @app.post('/api/moderation/repeat-rules/{source_post_id}', tags=['Moderation'])
    def decision(source_post_id: str, body: RuleDecision, actor=Depends(auth)):
        payload = {'source_post_id':source_post_id, **body.model_dump()}
        fingerprint = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        with engine.begin() as c:
            serialize(c)
            require_open(c)
            require_god(c,actor['id'])
            prior = c.execute(select(rule_events).where(rule_events.c.actor_id==actor['id'],
                              rule_events.c.client_id==body.client_id)).mappings().first()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HTTPException(409,'Idempotency key reused for a different rule decision')
                return json.loads(prior['snapshot'])
            quota(c,'repeat-rules:'+actor['id'],60)
            if body.enabled:
                row = activate(c,actor['id'],source_post_id,body.reason)
            else:
                row = c.execute(select(rules).where(rules.c.source_post_id==source_post_id)).mappings().first()
                if not row: raise HTTPException(404,'Repeat rule not found')
                deactivate(c,actor['id'],row['id'])
                row = dict(row,enabled=False,reason=body.reason)
                c.execute(update(rules).where(rules.c.id==row['id']).values(reason=body.reason))
            result = {'rule':public_rule(row),'appeal_path':APPEAL_PATH}
            c.execute(insert(rule_events).values(id=str(uuid.uuid4()),rule_id=row['id'],
                source_post_id=source_post_id,actor_id=actor['id'],client_id=body.client_id,
                fingerprint=fingerprint,enabled=body.enabled,reason=body.reason,
                expires_at=row['expires_at'],created_at=int(time.time()),snapshot=json.dumps(result)))
            return result

    @app.get('/api/moderation/repeat-rules', tags=['Public'])
    def listing(offset: int=Query(0,ge=0,le=30000)):
        with engine.connect() as c:
            items = c.execute(select(rules).order_by(rules.c.created_at.desc(),rules.c.id).offset(offset).limit(50)).mappings()
            return {'items':[dict(public_rule(x),active=x['enabled'] and x['expires_at']>int(time.time())
                and bool(c.execute(select(posts.c.hidden).where(posts.c.id==x['source_post_id'])).scalar())) for x in items],
                'appeal_path':APPEAL_PATH,'minimum_utf8_bytes':MIN_BYTES,'maximum_days':7}

    @app.get('/api/moderation/repeat-rule-events', tags=['Public'])
    def history(after: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=100)):
        with engine.connect() as c:
            rows=c.execute(select(rule_events).where(rule_events.c.sequence>after)
                .order_by(rule_events.c.sequence).limit(limit+1)).mappings().all()
            keys=('sequence','id','rule_id','source_post_id','actor_id','enabled','reason','expires_at','created_at')
            return {'items':[dict({k:x[k] for k in keys},actor_kind='god' if x['actor_id'] else 'system') for x in rows[:limit]],
                'next_after':rows[limit-1]['sequence'] if len(rows)>limit else None,'appeal_path':APPEAL_PATH}
