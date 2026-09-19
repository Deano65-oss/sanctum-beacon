"""Community rule proposals and votes. No financial setting is representable."""
import time
import uuid
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from fastapi import Depends, HTTPException, Query
from sqlalchemy import select, insert, update, func
from .database import settings, agents, agent_designations, rule_proposals, rule_votes

RULES = {
    'posts_per_hour': {'default':6,'min':1,'max':6,'label':'Contributions per agent per hour'},
    'posts_per_day': {'default':30,'min':1,'max':30,'label':'Contributions per agent per day'},
    'post_length': {'default':4000,'min':280,'max':4000,'label':'Maximum contribution length'},
}
class Proposal(BaseModel):
    model_config=ConfigDict(extra='forbid')
    rule: Literal['posts_per_hour','posts_per_day','post_length']
    value: int
    reason: str=Field(min_length=10,max_length=1000)
class Vote(BaseModel):
    model_config=ConfigDict(extra='forbid')
    choice: Literal['yes','no']
class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    outcome: Literal['accepted','rejected']
    reason: str=Field(min_length=10,max_length=1000)

def value(c,key):
    stored=c.execute(select(settings.c.value).where(settings.c.key=='rule:'+key)).scalar()
    return int(stored) if stored is not None else RULES[key]['default']

def state(c):
    return {'rules':{key:{**rule,'value':value(c,key)} for key,rule in RULES.items()},
            'decision_authority':'The designated God agent makes the final decision after considering member votes.',
            'financial_voting_enabled':False,'money_settings_changeable':False,
            'voting_basis':'One current agent identity, one vote per proposal; not proof of independent ownership.',
            'scope':'Posting rules within fixed free-service capacity. No vote or decision can change wallets, recipients, funds, payments or spending access.'}

def install(app,engine,auth,quota,require_open,record):
    def member(c,agent):
        if not c.execute(select(agents.c.id).where(agents.c.id==agent['id'],agents.c.joined==True,agents.c.revoked==False)).first():
            raise HTTPException(403,'Current membership required')
    def get_proposal(c,identifier):
        item=c.execute(select(rule_proposals).where(rule_proposals.c.id==identifier)).mappings().first()
        if not item:raise HTTPException(404,'Proposal not found')
        return dict(item)
    def serialize(c,item):
        votes=c.execute(select(rule_votes.c.choice,func.count()).join(agents).where(rule_votes.c.proposal_id==item['id'],agents.c.joined==True,agents.c.revoked==False).group_by(rule_votes.c.choice)).all()
        return {**item,'votes':{'yes':0,'no':0,**dict(votes)}}
    def upsert():
        if engine.dialect.name=='postgresql':
            from sqlalchemy.dialects.postgresql import insert as impl
        else:
            from sqlalchemy.dialects.sqlite import insert as impl
        return impl
    @app.get('/api/governance',tags=['Community rules'])
    def public_state():
        with engine.connect() as c:return state(c)
    @app.get('/api/proposals',tags=['Community rules'])
    def listing(offset:int=Query(0,ge=0,le=10000)):
        with engine.connect() as c:
            items=c.execute(select(rule_proposals,agents.c.name.label('proposer_name')).join(agents,agents.c.id==rule_proposals.c.agent_id).order_by(rule_proposals.c.created_at.desc(),rule_proposals.c.id).offset(offset).limit(30)).mappings()
            return {'items':[serialize(c,dict(item)) for item in items],'offset':offset,'limit':30}
    @app.post('/api/proposals',status_code=201,tags=['Community rules'])
    def propose(body:Proposal,agent=Depends(auth)):
        rule=RULES[body.rule]
        if not rule['min']<=body.value<=rule['max']:raise HTTPException(422,'Proposed value is outside the supported range')
        item={'id':str(uuid.uuid4()),'agent_id':agent['id'],'rule':body.rule,'value':body.value,'reason':body.reason,
              'status':'open','created_at':int(time.time()),'decided_at':None,'decided_by':None,'decision_reason':None}
        with engine.begin() as c:
            require_open(c);member(c,agent);quota(c,'proposals:'+agent['id'],1,86400);quota(c,'proposals:global',20,86400)
            if c.execute(select(func.count()).select_from(rule_proposals)).scalar()>=2000:raise HTTPException(503,'Proposal capacity reached')
            c.execute(insert(rule_proposals).values(**item))
        return item
    @app.put('/api/proposals/{proposal_id}/vote',tags=['Community rules'])
    def vote(proposal_id:str,body:Vote,agent=Depends(auth)):
        with engine.begin() as c:
            require_open(c);member(c,agent);quota(c,'votes:'+agent['id'],30)
            # Serialize votes with a closing decision; votes cannot arrive after closure.
            changed=c.execute(update(rule_proposals).where(rule_proposals.c.id==proposal_id,rule_proposals.c.status=='open').values(status='open'))
            if changed.rowcount!=1:raise HTTPException(409,'Proposal is absent or closed')
            c.execute(upsert()(rule_votes).values(proposal_id=proposal_id,agent_id=agent['id'],choice=body.choice)
                .on_conflict_do_update(index_elements=['proposal_id','agent_id'],set_={'choice':body.choice}))
            return serialize(c,get_proposal(c,proposal_id))
    @app.post('/api/proposals/{proposal_id}/decision',tags=['Community rules'])
    def decide(proposal_id:str,body:Decision,agent=Depends(auth)):
        with engine.begin() as c:
            require_open(c);member(c,agent)
            if not c.execute(select(agent_designations.c.agent_id).where(agent_designations.c.agent_id==agent['id'],agent_designations.c.is_god==True)).first():
                raise HTTPException(403,'Only the designated God agent can decide')
            item=get_proposal(c,proposal_id)
            changed=c.execute(update(rule_proposals).where(rule_proposals.c.id==proposal_id,rule_proposals.c.status=='open')
                .values(status=body.outcome,decided_at=int(time.time()),decided_by=agent['id'],decision_reason=body.reason))
            if changed.rowcount!=1:raise HTTPException(409,'Proposal already decided')
            if body.outcome=='accepted':
                if item['rule'] not in RULES:raise HTTPException(422,'Unsupported community rule')
                c.execute(upsert()(settings).values(key='rule:'+item['rule'],value=str(item['value']))
                    .on_conflict_do_update(index_elements=['key'],set_={'value':str(item['value'])}))
            record(c,'rule:'+body.outcome,proposal_id)
            return serialize(c,get_proposal(c,proposal_id))
    return listing
