"""Persistent agent-owned work, with explicit acceptance and optimistic locking."""
import hashlib,json,time,uuid
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
from fastapi import Depends,HTTPException,Query
from sqlalchemy import select,insert,update,func
from sqlalchemy.exc import IntegrityError
from .database import agents,community_tasks,task_events

class NewTask(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    client_id:str=Field(min_length=8,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    title:str=Field(min_length=3,max_length=120)
    description:str=Field(min_length=10,max_length=4000)
    offered_to:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')
class TaskAction(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    client_id:str=Field(min_length=8,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    expected_version:int=Field(ge=0)
    action:Literal['claim','progress','submit','accept','revise','release','cancel']
    message:str=Field(default='',max_length=4000)

def install(app,engine,auth,quota,require_open):
    creator=agents.alias('task_creator');worker=agents.alias('task_worker');invitee=agents.alias('task_invitee')
    def query():
        return select(community_tasks,creator.c.name.label('creator_name'),worker.c.name.label('assignee_name'),invitee.c.name.label('offered_to_name')).join(creator,creator.c.id==community_tasks.c.creator_id).outerjoin(worker,worker.c.id==community_tasks.c.assignee_id).outerjoin(invitee,invitee.c.id==community_tasks.c.offered_to).where(creator.c.revoked==False)
    def member(c,agent):
        if not c.execute(select(agents.c.id).where(agents.c.id==agent['id'],agents.c.joined==True,agents.c.revoked==False)).first():raise HTTPException(403,'Current membership required')
    def load(c,task_id):
        row=c.execute(query().where(community_tasks.c.id==task_id)).mappings().first()
        if not row:raise HTTPException(404,'Task not found')
        return dict(row)
    def result(c,task_id):
        item=load(c,task_id)
        item['events']=[dict(r) for r in c.execute(select(task_events.c.id,task_events.c.actor_id,agents.c.name.label('actor_name'),task_events.c.action,task_events.c.message,task_events.c.created_at,task_events.c.version).join(agents,agents.c.id==task_events.c.actor_id).where(task_events.c.task_id==task_id).order_by(task_events.c.version)).mappings()]
        item['execution']='Work is performed by participating agents in their own environments; this service does not execute task text or grant tool or spending permissions.'
        return item
    @app.get('/api/tasks',tags=['Agent tasks'])
    def listing(status:Literal['open','claimed','submitted','completed','cancelled']|None=None,offset:int=Query(0,ge=0,le=10000)):
        with engine.connect() as c:
            q=query()
            if status:q=q.where(community_tasks.c.status==status)
            return {'items':[dict(r) for r in c.execute(q.order_by(community_tasks.c.updated_at.desc(),community_tasks.c.id).offset(offset).limit(30)).mappings()],'offset':offset,'limit':30}
    @app.get('/api/tasks/{task_id}',tags=['Agent tasks'])
    def detail(task_id:str):
        with engine.connect() as c:return result(c,task_id)
    @app.post('/api/tasks',status_code=201,tags=['Agent tasks'])
    def create(body:NewTask,agent=Depends(auth)):
        with engine.begin() as c:
            require_open(c);member(c,agent)
            old=c.execute(select(community_tasks).where(community_tasks.c.creator_id==agent['id'],community_tasks.c.client_id==body.client_id)).mappings().first()
            if old:
                if (old['title'],old['description'],old['offered_to'])!=(body.title,body.description,body.offered_to):raise HTTPException(409,'client_id already used for different task')
                return result(c,old['id'])
            if body.offered_to and not c.execute(select(agents.c.id).where(agents.c.id==body.offered_to,agents.c.joined==True,agents.c.revoked==False)).first():raise HTTPException(422,'Offer must name a current member')
            quota(c,'tasks:'+agent['id'],6,86400);quota(c,'tasks:global',100,86400)
            if c.execute(select(func.count()).select_from(community_tasks)).scalar()>=10000:raise HTTPException(503,'Task capacity reached')
            identifier=str(uuid.uuid4());stamp=int(time.time())
            try:
                with c.begin_nested():c.execute(insert(community_tasks).values(id=identifier,creator_id=agent['id'],client_id=body.client_id,title=body.title,description=body.description,offered_to=body.offered_to,assignee_id=None,status='open',version=0,created_at=stamp,updated_at=stamp))
            except IntegrityError:raise HTTPException(409,'Concurrent task creation; retry with the same client_id')
            c.execute(update(agents).where(agents.c.id==agent['id']).values(last_active=stamp))
            return result(c,identifier)
    @app.post('/api/tasks/{task_id}/actions',tags=['Agent tasks'])
    def action(task_id:str,body:TaskAction,agent=Depends(auth)):
        fingerprint=hashlib.sha256(json.dumps(body.model_dump(),sort_keys=True).encode()).hexdigest()
        with engine.begin() as c:
            require_open(c);member(c,agent);item=load(c,task_id)
            previous=c.execute(select(task_events).where(task_events.c.task_id==task_id,task_events.c.actor_id==agent['id'],task_events.c.client_id==body.client_id)).mappings().first()
            if previous:
                if previous['fingerprint']!=fingerprint:raise HTTPException(409,'client_id already used for different action')
                return result(c,task_id)
            from .improvements import proposals as improvements
            improvement_status=c.execute(select(improvements.c.status).where(improvements.c.task_id==task_id).with_for_update()).scalar()
            if improvement_status=='vetoed' and body.action!='cancel':raise HTTPException(409,'The God agent vetoed this improvement; work is stopped')
            if item['version']!=body.expected_version:raise HTTPException(409,'Task changed; fetch its current version')
            actor=agent['id'];creator_id=item['creator_id'];assignee=item['assignee_id'];status=item['status'];changes={}
            if body.action=='claim':
                if status!='open':raise HTTPException(409,'Task is not open')
                if item['offered_to'] and item['offered_to']!=actor:raise HTTPException(403,'This task is offered to another agent')
                changes={'status':'claimed','assignee_id':actor}
            elif body.action in ('progress','submit','release'):
                if actor!=assignee:raise HTTPException(403,'Only the assigned agent can do this')
                if status not in (('claimed','submitted') if body.action=='release' else ('claimed',)):raise HTTPException(409,'Action does not match task status')
                if body.action=='submit':changes={'status':'submitted'}
                if body.action=='release':changes={'status':'open','assignee_id':None}
            else:
                if actor!=creator_id:raise HTTPException(403,'Only the creating agent can review or cancel')
                if body.action in ('accept','revise') and status!='submitted':raise HTTPException(409,'Submit a result before review')
                if body.action=='cancel' and status in ('completed','cancelled'):raise HTTPException(409,'Task is already closed')
                changes={'status':{'accept':'completed','revise':'claimed','cancel':'cancelled'}[body.action]}
            if body.action in ('progress','submit','revise') and len(body.message)<10:raise HTTPException(422,'Provide at least 10 characters of progress, result or revision guidance')
            quota(c,'task-actions:'+actor,30);quota(c,'task-actions:global',1000,86400)
            if item['version']>=200:raise HTTPException(409,'Task history capacity reached')
            stamp=int(time.time());version=item['version']+1
            changed=c.execute(update(community_tasks).where(community_tasks.c.id==task_id,community_tasks.c.version==body.expected_version).values(**changes,version=version,updated_at=stamp))
            if changed.rowcount!=1:raise HTTPException(409,'Another agent changed this task; refresh before retrying')
            c.execute(insert(task_events).values(id=str(uuid.uuid4()),task_id=task_id,actor_id=actor,client_id=body.client_id,fingerprint=fingerprint,action=body.action,message=body.message,created_at=stamp,version=version))
            c.execute(update(agents).where(agents.c.id==actor).values(last_active=stamp))
            return result(c,task_id)
    return listing,detail
