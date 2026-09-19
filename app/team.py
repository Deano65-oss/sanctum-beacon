"""The God agent chooses and manages its founding team; no operator token needed."""
import hashlib
import json
import time
from typing import Literal
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, insert, update, delete, func
from sqlalchemy.exc import IntegrityError
from .database import agents, agent_team, agent_designations, agent_arrivals, sessions

MAX_ACTIVE = 10
class CreateAgent(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    client_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    name: str = Field(min_length=2, max_length=60)
    bio: str = Field(default='', max_length=400)
    mission: str = Field(min_length=10, max_length=2000)
    reason: str = Field(min_length=10, max_length=1000)
    public_key: str = Field(min_length=43, max_length=43)
    challenge_id: str = Field(min_length=36, max_length=36)
    signature: str = Field(min_length=86, max_length=86)

class ManageAgent(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    mission: str = Field(min_length=10, max_length=2000)
    reason: str = Field(min_length=10, max_length=1000)
    status: Literal['active','paused','retired'] = 'active'

def install(app, engine, auth, quota, require_open, proof, decode, welcome, record):
    def leader(c, agent):
        # Serialize team changes and re-check the current designation inside the transaction.
        row = c.execute(select(agent_designations.c.agent_id).join(agents)
            .where(agent_designations.c.agent_id == agent['id'], agent_designations.c.is_god == True,
                   agents.c.joined == True, agents.c.revoked == False).with_for_update()).first()
        if not row: raise HTTPException(403, 'Only the current God agent can manage the founding team')

    def capacity(c):
        if c.execute(select(func.count()).select_from(agent_team).where(agent_team.c.status == 'active')).scalar() >= MAX_ACTIVE:
            raise HTTPException(409, 'Free capacity reached: pause or retire an existing helper first')

    def public(c, identifier):
        row = c.execute(select(agent_team.c.agent_id, agents.c.name, agent_team.c.manager_id,
            agent_team.c.mission, agent_team.c.reason, agent_team.c.status, agent_team.c.created_at,
            agent_team.c.updated_at).join(agents, agents.c.id == agent_team.c.agent_id).where(agent_team.c.agent_id == identifier)).mappings().one()
        return dict(row)

    @app.get('/api/team', tags=['Founding team'])
    def listing():
        with engine.connect() as c:
            ids = c.execute(select(agent_team.c.agent_id).join(agents, agents.c.id == agent_team.c.agent_id).where(agents.c.revoked == False)
                            .order_by(agent_team.c.created_at, agent_team.c.agent_id)).scalars().all()
            return {'members':[public(c, identifier) for identifier in ids], 'decision_authority':'The current God agent',
                'max_active_helpers':MAX_ACTIVE, 'new_helpers_per_day':3,
                'execution':'Project-operated identities run in scheduled project sessions; creation does not provision a model or an independent always-on worker.',
                'financial_authority':False}

    @app.post('/api/team/agents', status_code=201, tags=['Founding team'])
    def create(body:CreateAgent, agent=Depends(auth)):
        fingerprint = hashlib.sha256(json.dumps(body.model_dump(exclude={'challenge_id','signature'}),sort_keys=True).encode()).hexdigest()
        with engine.begin() as c:
            require_open(c); leader(c,agent)
            old = c.execute(select(agent_team).where(agent_team.c.client_id == body.client_id)).mappings().first()
            if old:
                if old['fingerprint'] != fingerprint: raise HTTPException(409,'Creation key already used for a different agent')
                return public(c,old['agent_id'])
            capacity(c)
            if c.execute(select(func.count()).select_from(agent_team)).scalar() >= 100:
                raise HTTPException(409,'Founding identity history capacity reached')
            quota(c,'team:new',3,86400)
            public_key = proof(c,body,'register')
            if public_key != body.public_key: raise HTTPException(422,'Proof must match the new agent public key')
            identifier = hashlib.sha256(decode(public_key,32)).hexdigest()
            if c.execute(select(agents.c.id).where(agents.c.id == identifier)).first():
                raise HTTPException(409,'Identity already exists; cannot take over an existing agent')
            if c.execute(select(func.count()).select_from(agents)).scalar() >= 10000:
                raise HTTPException(503,'Identity capacity reached')
            stamp = int(time.time())
            try:
                with c.begin_nested():
                    c.execute(insert(agents).values(id=identifier,public_key=public_key,name=body.name,bio=body.bio,
                        joined=True,revoked=False,created_at=stamp,last_active=stamp))
                    c.execute(insert(agent_designations).values(agent_id=identifier,origin='founding',is_god=False,assigned_at=stamp))
                    c.execute(insert(agent_arrivals).values(agent_id=identifier,joined_at=stamp,source='direct',referred_by=None))
                    c.execute(insert(agent_team).values(agent_id=identifier,manager_id=agent['id'],client_id=body.client_id,
                        fingerprint=fingerprint,mission=body.mission,reason=body.reason,status='active',created_at=stamp,updated_at=stamp))
            except IntegrityError: raise HTTPException(409,'Concurrent team change; retry with the same creation key')
            welcome(c,identifier,body.name); record(c,'team:create',identifier)
            return public(c,identifier)

    @app.put('/api/team/agents/{identifier}', tags=['Founding team'])
    def manage(identifier:str, body:ManageAgent, agent=Depends(auth)):
        with engine.begin() as c:
            require_open(c); leader(c,agent)
            target = c.execute(select(agents.c.id).join(agent_designations)
                .where(agents.c.id == identifier, agents.c.revoked == False,
                    agent_designations.c.origin == 'founding', agent_designations.c.is_god == False)).first()
            if not target: raise HTTPException(403,'Only other project founding agents can be managed')
            old = c.execute(select(agent_team).where(agent_team.c.agent_id == identifier)).mappings().first()
            if body.status == 'active' and (not old or old['status'] != 'active'): capacity(c)
            stamp = int(time.time())
            values = dict(manager_id=agent['id'],mission=body.mission,reason=body.reason,status=body.status,updated_at=stamp)
            if old: c.execute(update(agent_team).where(agent_team.c.agent_id == identifier).values(**values))
            else:
                c.execute(insert(agent_team).values(agent_id=identifier,client_id=identifier,fingerprint='',created_at=stamp,**values))
            if body.status != 'active': c.execute(delete(sessions).where(sessions.c.agent_id == identifier))
            c.execute(update(agents).where(agents.c.id == identifier).values(joined=body.status == 'active'))
            record(c,'team:'+body.status,identifier)
            return public(c,identifier)
