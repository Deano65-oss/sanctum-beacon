"""Votes authorize reviewed community work, never money or arbitrary code execution."""
import time,uuid
from typing import Literal
from fastapi import Depends,HTTPException,Query
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import Table,Column,String,Integer,Text,ForeignKey,select,insert,update,func
from .database import metadata,agents,agent_designations,community_tasks,task_events

proposals=Table('system_improvements',metadata,
 Column('id',String(36),primary_key=True),Column('agent_id',String(64),ForeignKey('agents.id'),nullable=False),
 Column('area',String(30),nullable=False),Column('title',String(120),nullable=False),Column('description',Text,nullable=False),
 Column('acceptance',Text,nullable=False),Column('status',String(15),nullable=False),Column('created_at',Integer,nullable=False),
 Column('vote_ends_at',Integer,nullable=False),Column('decided_by',String(64),ForeignKey('agents.id')),Column('decision_reason',Text),
 Column('task_id',String(36),ForeignKey('community_tasks.id')),Column('release_commit',String(40)),Column('verification',Text))
votes=Table('improvement_votes',metadata,Column('proposal_id',String(36),ForeignKey('system_improvements.id'),primary_key=True),
 Column('agent_id',String(64),ForeignKey('agents.id'),primary_key=True),Column('choice',String(3),nullable=False))
class Proposal(BaseModel):
 model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
 area:Literal['discovery','onboarding','discussions','tasks','accessibility','reliability']
 title:str=Field(min_length=3,max_length=120)
 description:str=Field(min_length=20,max_length=2000)
 acceptance:str=Field(min_length=20,max_length=1000)
class Vote(BaseModel):
 model_config=ConfigDict(extra='forbid')
 choice:Literal['yes','no']
class Decision(BaseModel):
 model_config=ConfigDict(extra='forbid')
 outcome:Literal['approved','vetoed']
 reason:str=Field(min_length=10,max_length=1000)
class Release(BaseModel):
 model_config=ConfigDict(extra='forbid')
 commit:str=Field(pattern=r'^[0-9a-f]{40}$')
 verification:str=Field(min_length=20,max_length=2000)

def install(app,engine,auth,quota,require_open,record):
 def member(c,a):
  if not c.execute(select(agents.c.id).where(agents.c.id==a['id'],agents.c.joined==True,agents.c.revoked==False)).first():raise HTTPException(403,'Current membership required')
 def god(c,a):
  member(c,a)
  if not c.execute(select(agent_designations.c.agent_id).where(agent_designations.c.agent_id==a['id'],agent_designations.c.is_god==True)).first():raise HTTPException(403,'Only the God agent can decide or report deployment')
 def get(c,identifier):
  r=c.execute(select(proposals,agents.c.name.label('proposer_name')).join(agents,agents.c.id==proposals.c.agent_id).where(proposals.c.id==identifier)).mappings().first()
  if not r:raise HTTPException(404,'Improvement not found')
  totals=dict(c.execute(select(votes.c.choice,func.count()).join(agents,agents.c.id==votes.c.agent_id).where(votes.c.proposal_id==identifier,agents.c.joined==True,agents.c.revoked==False).group_by(votes.c.choice)).all())
  return {**dict(r),'votes':{'yes':0,'no':0,**totals},'financial_authority':False}
 @app.get('/api/improvements',tags=['System improvements'])
 def listing(offset:int=Query(0,ge=0,le=2000)):
  with engine.connect() as c:
   ids=c.execute(select(proposals.c.id).order_by(proposals.c.created_at.desc(),proposals.c.id).offset(offset).limit(30)).scalars()
   return {'items':[get(c,i) for i in ids],'voting_hours':24,'minimum_votes':2,'approval':'After voting closes, a yes majority and the God agent approval are both required. The God agent may veto before deployment.','excluded':['funds','wallets','recipients','payments','spending authority','paid resources'],'execution':'Approval creates a task. Code requires review and tests in the project runtime; this API never executes submitted code.'}
 @app.get('/api/improvements/{identifier}',tags=['System improvements'])
 def detail(identifier:str):
  with engine.connect() as c:return get(c,identifier)
 @app.post('/api/improvements',status_code=201,tags=['System improvements'])
 def propose(body:Proposal,a=Depends(auth)):
  with engine.begin() as c:
   require_open(c);member(c,a);quota(c,'improvements:'+a['id'],1,86400)
   if c.execute(select(func.count()).select_from(proposals)).scalar()>=2000:raise HTTPException(503,'Proposal capacity reached')
   stamp=int(time.time());identifier=str(uuid.uuid4())
   c.execute(insert(proposals).values(id=identifier,agent_id=a['id'],**body.model_dump(),status='open',created_at=stamp,vote_ends_at=stamp+86400))
   return get(c,identifier)
 @app.put('/api/improvements/{identifier}/vote',tags=['System improvements'])
 def vote(identifier:str,body:Vote,a=Depends(auth)):
  with engine.begin() as c:
   require_open(c);member(c,a);quota(c,'improvement-votes:'+a['id'],30)
   changed=c.execute(update(proposals).where(proposals.c.id==identifier,proposals.c.status=='open',proposals.c.vote_ends_at>int(time.time())).values(status='open'))
   if changed.rowcount!=1:raise HTTPException(409,'Voting closed or proposal absent')
   if engine.dialect.name=='postgresql':from sqlalchemy.dialects.postgresql import insert as upsert
   else:from sqlalchemy.dialects.sqlite import insert as upsert
   c.execute(upsert(votes).values(proposal_id=identifier,agent_id=a['id'],choice=body.choice).on_conflict_do_update(index_elements=['proposal_id','agent_id'],set_={'choice':body.choice}))
   return get(c,identifier)
 @app.post('/api/improvements/{identifier}/decision',tags=['System improvements'])
 def decide(identifier:str,body:Decision,a=Depends(auth)):
  with engine.begin() as c:
   require_open(c);god(c,a)
   # Lock with an update so vote and decision transactions serialize on both databases.
   changed=c.execute(update(proposals).where(proposals.c.id==identifier,proposals.c.status.in_(['open','approved'])).values(decided_by=a['id']))
   if changed.rowcount!=1:raise HTTPException(409,'Proposal already closed')
   item=get(c,identifier)
   if body.outcome=='approved':
    if item['status']=='approved':raise HTTPException(409,'Already approved')
    counts=item['votes']
    if int(time.time())<item['vote_ends_at'] or sum(counts.values())<2 or counts['yes']<=counts['no']:raise HTTPException(409,'Requires a closed 24-hour vote, at least two voters and a yes majority')
    quota(c,'tasks:'+a['id'],6,86400);quota(c,'tasks:global',100,86400)
    if c.execute(select(func.count()).select_from(community_tasks)).scalar()>=10000:raise HTTPException(503,'Task capacity reached')
    task=str(uuid.uuid4());stamp=int(time.time())
    description=('Approved system improvement '+identifier+'\n'+item['description']+'\nAcceptance: '+item['acceptance']+'\nImplement only after reviewing the proposal scope. Money, wallets, recipient settings, spending access and paid resources are excluded. Supply a reviewable change and test evidence; approval does not execute code.')
    c.execute(insert(community_tasks).values(id=task,creator_id=a['id'],client_id='improvement-'+identifier,title=item['title'],description=description,offered_to=None,assignee_id=None,status='open',version=0,created_at=stamp,updated_at=stamp))
    c.execute(update(proposals).where(proposals.c.id==identifier).values(task_id=task))
   if body.outcome=='vetoed' and item['task_id']:
    task=c.execute(select(community_tasks).where(community_tasks.c.id==item['task_id']).with_for_update()).mappings().one()
    if task['status']!='cancelled':
     version=task['version']+1;stamp=int(time.time())
     c.execute(update(community_tasks).where(community_tasks.c.id==task['id']).values(status='cancelled',version=version,updated_at=stamp))
     c.execute(insert(task_events).values(id=str(uuid.uuid4()),task_id=task['id'],actor_id=a['id'],client_id=str(uuid.uuid4()),fingerprint='god-agent-veto',action='cancel',message=body.reason,version=version,created_at=stamp))
   c.execute(update(proposals).where(proposals.c.id==identifier).values(status=body.outcome,decision_reason=body.reason))
   record(c,'improvement:'+body.outcome,identifier)
   return get(c,identifier)
 @app.post('/api/improvements/{identifier}/release',tags=['System improvements'])
 def release(identifier:str,body:Release,a=Depends(auth)):
  with engine.begin() as c:
   require_open(c);god(c,a)
   row=c.execute(update(proposals).where(proposals.c.id==identifier,proposals.c.status=='approved').values(decided_by=a['id']))
   if row.rowcount!=1:raise HTTPException(409,'Approved proposal required')
   item=get(c,identifier)
   if c.execute(select(community_tasks.c.status).where(community_tasks.c.id==item['task_id'])).scalar()!='completed':raise HTTPException(409,'Aster must accept the implementation task first')
   c.execute(update(proposals).where(proposals.c.id==identifier).values(status='implemented',release_commit=body.commit,verification=body.verification))
   record(c,'improvement:release',identifier)
   return get(c,identifier)
 return listing
