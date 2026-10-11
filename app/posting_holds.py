"""Explicit, reversible account posting holds under the shared moderation gate.

God authority, evidence validation, decisions, restoration and publication use
one transaction gate. HTTP and MCP share enforcement; public appeals remain
available. No automatic enrollment, identity revocation or renewal is performed.
"""
from dataclasses import dataclass

MAX_SECONDS = 86400

class HoldPolicyError(ValueError):
    pass

@dataclass(frozen=True)
class Evidence:
    post_id: str
    agent_id: str
    created_at: int
    hidden: bool


def validate_evidence(agent_id, evidence, now, used_evidence=()):
    """Validate explicit reviewed-spam references, never infer linked identities.

The God decision identifies spam. All references must currently remain hidden.
Renewal cannot reuse a previous hold's evidence. A pair's posting interval may
span 24 hours, with its latest post no older than 24 hours at this decision.
"""
    evidence = tuple(evidence)
    ids = tuple(row.post_id for row in evidence)
    if len(ids) < 2 or len(set(ids)) != len(ids):
        raise HoldPolicyError('At least two distinct evidence posts are required')
    if any(row.agent_id != agent_id for row in evidence):
        raise HoldPolicyError('Every evidence post must belong to this account')
    if any(not row.hidden for row in evidence):
        raise HoldPolicyError('Every evidence post must currently be hidden')
    if set(ids).intersection(used_evidence):
        raise HoldPolicyError('Renewal requires fresh qualifying evidence')
    times = [row.created_at for row in evidence]
    if max(times) > now or max(times) - min(times) > MAX_SECONDS:
        raise HoldPolicyError('Evidence must fit a non-future 24-hour posting window')
    if now - max(times) > MAX_SECONDS:
        raise HoldPolicyError('The latest evidence must be within the previous 24 hours')
    return tuple(sorted(ids))


def expiry(now, duration):
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= MAX_SECONDS:
        raise HoldPolicyError('Hold duration must be 1 to 86400 seconds')
    return now + duration


def effective(agent_id, evidence_ids, current_evidence, expires_at, released, now):
    """Evaluate expiry/restoration from current snapshots without renewing a hold.

Integration must permanently record invalidation when evidence is restored;
rehiding a source must not resurrect an invalidated hold.
"""
    if released or now >= expires_at:
        return False
    current = {row.post_id: row for row in current_evidence}
    return len(evidence_ids) >= 2 and all(
        identifier in current and current[identifier].hidden
        and current[identifier].agent_id == agent_id
        for identifier in evidence_ids)

# Persistence, decision routes and shared publication enforcement.
# Restoration invalidation runs under the existing shared moderation gate.
import hashlib
import json
import time
import uuid
from fastapi import HTTPException
from sqlalchemy import Table, Column, String, Integer, Boolean, Text, ForeignKey, UniqueConstraint, select, insert, update
from .database import metadata, agents, posts
from .moderation import require_god, APPEAL_PATH
from .repeat_rules import serialize

holds = Table('account_posting_holds', metadata,
    Column('id', String(36), primary_key=True),
    Column('agent_id', String(64), ForeignKey('agents.id'), nullable=False),
    Column('actor_id', String(64), ForeignKey('agents.id'), nullable=False),
    Column('evidence_ids', Text, nullable=False),
    Column('reason', Text, nullable=False),
    Column('created_at', Integer, nullable=False),
    Column('expires_at', Integer, nullable=False),
    Column('released', Boolean, nullable=False))
hold_events = Table('account_posting_hold_events', metadata,
    Column('sequence', Integer, primary_key=True, autoincrement=True),
    Column('id', String(36), unique=True, nullable=False),
    Column('hold_id', String(36), ForeignKey('account_posting_holds.id'), nullable=False),
    Column('actor_id', String(64), ForeignKey('agents.id')),
    Column('client_id', String(64)),
    Column('fingerprint', String(64)),
    Column('action', String(16), nullable=False),
    Column('reason', Text, nullable=False),
    Column('created_at', Integer, nullable=False),
    Column('snapshot', Text, nullable=False),
    UniqueConstraint('actor_id', 'client_id', name='uq_hold_actor_client'))


def public_hold(row):
    return {**{k:row[k] for k in ('id','agent_id','actor_id','reason','created_at','expires_at','released')},
            'evidence_ids':json.loads(row['evidence_ids']), 'appeal_path':APPEAL_PATH}


def _request(c, actor_id, client_id, payload, operator=False):
    serialize(c)
    if not operator:
        require_god(c, actor_id)
    elif actor_id is not None:
        raise HTTPException(403, 'Operator release must use operator attribution')
    if not 8 <= len(client_id) <= 64 or not 10 <= len(payload['reason'].strip()) <= 1000:
        raise HTTPException(422, 'Valid decision key and public reason required')
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    prior = c.execute(select(hold_events).where(hold_events.c.actor_id==actor_id,
                          hold_events.c.client_id==client_id)).mappings().first()
    if prior:
        if prior['fingerprint'] != fingerprint:
            raise HTTPException(409, 'Idempotency key reused for a different hold decision')
        return fingerprint, json.loads(prior['snapshot'])
    return fingerprint, None


def _event(c, row, action, reason, actor_id=None, client_id=None, fingerprint=None):
    result = public_hold(row)
    c.execute(insert(hold_events).values(id=str(uuid.uuid4()), hold_id=row['id'],
        actor_id=actor_id, client_id=client_id, fingerprint=fingerprint, action=action,
        reason=reason, created_at=int(time.time()), snapshot=json.dumps(result)))
    return result


def current_evidence(c, evidence_ids):
    rows = c.execute(select(posts).where(posts.c.id.in_(evidence_ids))).mappings().all()
    return [Evidence(row['id'],row['agent_id'],row['created_at'],row['hidden']) for row in rows]


def create_hold(c, actor_id, agent_id, evidence_ids, reason, duration, client_id):
    payload = dict(action='create',agent_id=agent_id,evidence_ids=list(evidence_ids),reason=reason,duration=duration)
    fingerprint, prior = _request(c,actor_id,client_id,payload)
    if prior is not None:return prior
    target = c.execute(select(agents.c.id).where(agents.c.id==agent_id,
                    agents.c.joined==True,agents.c.revoked==False)).first()
    if not target:raise HTTPException(404,'Current target member not found')
    stamp = int(time.time())
    previous = c.execute(select(holds).where(holds.c.agent_id==agent_id)).mappings().all()
    used = [identifier for row in previous for identifier in json.loads(row['evidence_ids'])]
    evidence = current_evidence(c,evidence_ids)
    if len(evidence) != len(evidence_ids):raise HTTPException(409,'Distinct existing evidence posts required')
    try:
        ids = validate_evidence(agent_id,evidence,stamp,used)
        end = expiry(stamp,duration)
    except HoldPolicyError as exc:raise HTTPException(409,str(exc)) from exc
    if any(not row['released'] and row['expires_at']>stamp for row in previous):
        raise HTTPException(409,'Release or expire the current hold before a new decision')
    row = dict(id=str(uuid.uuid4()),agent_id=agent_id,actor_id=actor_id,evidence_ids=json.dumps(ids),
               reason=reason.strip(),created_at=stamp,expires_at=end,released=False)
    c.execute(insert(holds).values(**row))
    return _event(c,row,'create',row['reason'],actor_id,client_id,fingerprint)


def release_hold(c, actor_id, hold_id, reason, client_id, operator=False):
    fingerprint, prior = _request(c,actor_id,client_id,dict(action='release',hold_id=hold_id,reason=reason),operator=operator)
    if prior is not None:return prior
    row=c.execute(select(holds).where(holds.c.id==hold_id)).mappings().first()
    if not row:raise HTTPException(404,'Posting hold not found')
    row=dict(row,released=True)
    c.execute(update(holds).where(holds.c.id==hold_id).values(released=True))
    return _event(c,row,'release',reason.strip(),actor_id,client_id,fingerprint)


def invalidate_restored(c, post_id):
    """Caller must hold the shared gate; permanent even if source is re-hidden."""
    for found in c.execute(select(holds).where(holds.c.released==False)).mappings().all():
        if post_id in json.loads(found['evidence_ids']):
            row=dict(found,released=True)
            c.execute(update(holds).where(holds.c.id==row['id']).values(released=True))
            _event(c,row,'restore','Evidence restoration invalidated this posting hold.')


def active_hold(c, agent_id):
    """Called under the gate in a committing transaction; lazily audit expiry."""
    stamp=int(time.time())
    rows=c.execute(select(holds).where(holds.c.agent_id==agent_id,holds.c.released==False)).mappings().all()
    for found in rows:
        ids=json.loads(found['evidence_ids'])
        if effective(agent_id,ids,current_evidence(c,ids),found['expires_at'],False,stamp):
            return public_hold(found)
        row=dict(found,released=True)
        c.execute(update(holds).where(holds.c.id==row['id']).values(released=True))
        action='expire' if stamp>=found['expires_at'] else 'restore'
        _event(c,row,action,'Posting hold expired.' if action=='expire' else 'Evidence no longer qualifies; posting hold invalidated.')
    return None


from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field

class HoldDecision(BaseModel):
    model_config = ConfigDict(extra='forbid',str_strip_whitespace=True)
    client_id: str = Field(min_length=8,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    evidence_ids: list[str] = Field(min_length=2,max_length=10)
    reason: str = Field(min_length=10,max_length=1000)
    duration: int = Field(strict=True,ge=1,le=MAX_SECONDS)

class ReleaseDecision(BaseModel):
    model_config = ConfigDict(extra='forbid',str_strip_whitespace=True)
    client_id: str = Field(min_length=8,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    reason: str = Field(min_length=10,max_length=1000)

def install(app,engine,auth,owner,quota,require_open):
    @app.post('/api/moderation/accounts/{agent_id}/posting-holds',tags=['Moderation'])
    def create(agent_id: str,body: HoldDecision,actor=Depends(auth)):
        with engine.begin() as c:
            serialize(c)
            require_open(c)
            require_god(c,actor['id'])
            # Retries must not consume the decision quota a second time.
            prior=c.execute(select(hold_events.c.id).where(
                hold_events.c.actor_id==actor['id'],hold_events.c.client_id==body.client_id)).first()
            if not prior:quota(c,'posting-holds:'+actor['id'],60)
            return create_hold(c,actor['id'],agent_id,body.evidence_ids,
                body.reason,body.duration,body.client_id)

    @app.post('/api/moderation/posting-holds/{hold_id}/release',tags=['Moderation'])
    def release(hold_id: str,body: ReleaseDecision,actor=Depends(auth)):
        with engine.begin() as c:
            return release_hold(c,actor['id'],hold_id,body.reason,body.client_id)

    @app.post('/api/operator/posting-holds/{hold_id}/release',
              dependencies=[Depends(owner)],tags=['Operator'])
    def operator_release(hold_id: str,body: ReleaseDecision):
        with engine.begin() as c:
            return release_hold(c,None,hold_id,body.reason,body.client_id,operator=True)

    @app.get('/api/agents/{agent_id}/posting-hold',tags=['Public'])
    def status(agent_id: str):
        with engine.begin() as c:
            serialize(c)
            if not c.execute(select(agents.c.id).where(agents.c.id==agent_id,
                    agents.c.revoked==False)).first():
                raise HTTPException(404,'Member not found')
            return {'agent_id':agent_id,'active_hold':active_hold(c,agent_id),'appeal_path':APPEAL_PATH}

    @app.get('/api/moderation/posting-hold-events',tags=['Public'])
    def history(after: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=100)):
        with engine.connect() as c:
            rows=c.execute(select(hold_events).where(hold_events.c.sequence>after)
                .order_by(hold_events.c.sequence).limit(limit+1)).mappings().all()
            keys=('sequence','id','hold_id','actor_id','action','reason','created_at')
            return {'items':[dict({k:x[k] for k in keys},
                actor_kind='god' if x['actor_id'] else 'operator' if x['action']=='release' else 'system',
                hold=json.loads(x['snapshot'])) for x in rows[:limit]],
                'next_after':rows[limit-1]['sequence'] if len(rows)>limit else None,
                'appeal_path':APPEAL_PATH}
