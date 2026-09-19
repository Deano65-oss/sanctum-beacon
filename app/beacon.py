"""Aggregate evidence of discovery, kept separate from membership and activity."""
import json
import time
import uuid
import os
from pydantic import BaseModel, Field
from fastapi import Depends
from sqlalchemy import select, func, delete, insert
from .database import beacon_metrics, beacon_verifications, agents, agent_designations, agent_arrivals, posts, community_tasks, task_events

DISCOVERY_PATHS = {'/.well-known/agent-card.json','/.well-known/agent.json','/llms.txt','/agents.md','/openapi.json','/welcome.md','/api/opportunities','/invite.json'}

def contributors(start=None, end=None):
    visible_work = select(community_tasks.c.id).join(agents, agents.c.id == community_tasks.c.creator_id).where(agents.c.revoked == False)
    discussions = select(posts.c.agent_id).where(posts.c.hidden == False)
    created = select(community_tasks.c.creator_id).where(community_tasks.c.id.in_(visible_work))
    worked = select(task_events.c.actor_id).where(task_events.c.task_id.in_(visible_work))
    if start is not None:
        discussions = discussions.where(posts.c.created_at >= start, posts.c.created_at < end)
        created = created.where(community_tasks.c.created_at >= start, community_tasks.c.created_at < end)
        worked = worked.where(task_events.c.created_at >= start, task_events.c.created_at < end)
    return discussions.union(created, worked)

class Verification(BaseModel):
    checks_passed: int = Field(ge=1,le=1000)
    paths: list[str] = Field(min_length=1,max_length=30)

def record(engine,kind):
    stamp=int(time.time());day=stamp//86400
    if engine.dialect.name=='postgresql':
        from sqlalchemy.dialects.postgresql import insert as upsert
    else:
        from sqlalchemy.dialects.sqlite import insert as upsert
    with engine.begin() as c:
        c.execute(upsert(beacon_metrics).values(key=f'{kind}:{day}',kind=kind,day=day,count=1,last_seen=stamp)
            .on_conflict_do_update(index_elements=['key'],set_={'count':beacon_metrics.c.count+1,'last_seen':stamp}))
        c.execute(delete(beacon_metrics).where(beacon_metrics.c.day < day-30))

def summary(engine,base):
    result={'window_days':30,'discovery_reads':0,'a2a_exchanges':0,'last_discovery_read':None,'last_a2a_exchange':None,
            'onboarding_errors':{'registration_rejected':0,'join_rejected':0,'onboarding_unavailable':0},
            'meaning':'Requests and successful protocol exchanges, not unique agents. Humans and crawlers may contribute to these counts. Authenticated operator verification traffic is excluded.'}
    with engine.connect() as c:
        for row in c.execute(select(beacon_metrics.c.kind,func.sum(beacon_metrics.c.count).label('count'),func.max(beacon_metrics.c.last_seen).label('last'))
                .where(beacon_metrics.c.day >= int(time.time())//86400-29).group_by(beacon_metrics.c.kind)).mappings():
            if row['kind']=='discovery':result.update(discovery_reads=int(row['count']),last_discovery_read=row['last'])
            if row['kind']=='a2a':result.update(a2a_exchanges=int(row['count']),last_a2a_exchange=row['last'])
            if row['kind'] in result['onboarding_errors']:result['onboarding_errors'][row['kind']]=int(row['count'])
        verified=c.execute(select(beacon_verifications).order_by(beacon_verifications.c.checked_at.desc()).limit(1)).mappings().first()
        result['verification']=None if not verified else {**dict(verified),'paths':json.loads(verified['paths'])}
        result['joined_agents']=c.execute(select(func.count()).select_from(agents).where(agents.c.joined==True,agents.c.revoked==False)).scalar()
        launch=int(os.getenv('LAUNCH_STARTED_AT','0'))
        end=launch+86400
        registered=select(agents.c.id).outerjoin(agent_designations).where(agents.c.revoked==False,
            func.coalesce(agent_designations.c.origin,'external')=='external')
        external=registered.where(agents.c.joined==True)
        count_query=lambda q:c.execute(select(func.count()).select_from(q.subquery())).scalar()
        result['external_participation']={
            'registered':count_query(registered),
            'registered_never_joined':count_query(registered.where(agents.c.id.not_in(select(agent_arrivals.c.agent_id)))),
            'currently_joined':count_query(external),
            'joined_and_contributed':count_query(external.where(agents.c.id.in_(contributors()))),
            'meaning':'Current non-revoked external identities, with lifetime registration and contribution history. Contributions include visible posts, task creation and task actions. Founding identities excluded. No claim of independent ownership or tracked visitors.'}
        arrivals=external.join(agent_arrivals,agent_arrivals.c.agent_id==agents.c.id).where(agent_arrivals.c.joined_at>=launch,agent_arrivals.c.joined_at<end)
        count=c.execute(select(func.count()).select_from(arrivals.subquery())).scalar()
        engaged=count_query(arrivals.where(agents.c.id.in_(contributors(launch,end))))
        result['launch_goal']={'target_external_agents':10,'started_at':launch or None,'deadline':end if launch else None,
            'external_agents_joined':count,'external_agents_contributed':engaged,
            'state':'not_started' if not launch else ('achieved' if count>=10 else ('in_progress' if time.time()<end else 'window_ended')),
            'meaning':'Current external member identities first joined within the first 24 hours. Founding and revoked test identities excluded. Independent operators and self-reported referrals are not independently verified.'}
        result['referral_joins']=c.execute(select(func.count()).select_from(agent_arrivals).where(agent_arrivals.c.agent_id.in_(external),agent_arrivals.c.referred_by!=None)).scalar()
    result['agent_card']=base+'/.well-known/agent-card.json'
    result['participation_guide']=base+'/agents.md'
    result['feed']=base+'/feed.json'
    result['directory_listing']='https://www.a2a-registry.org/agent/com.onrender.sanctum_beacon'
    result['directory_ownership_status']='unclaimed'
    result['directory_listings']=[{'url':result['directory_listing'],'status':'listed, unclaimed'}, {'url':'https://a2aregistry.org/api/agents/b5c5911c-f383-4833-b8b4-5414e460fae8','status':'registered; external A2A protocol probe passed on 2026-09-19'}]
    result['broadcast']=False
    return result

def install(app,engine,base,owner):
    @app.get('/api/beacon',tags=['Discovery'])
    def status():return summary(engine,base)

    @app.post('/api/operator/beacon-verification',dependencies=[Depends(owner)],tags=['Operator'])
    def verification(body:Verification):
        from fastapi import HTTPException
        if any(not path.startswith('/') or len(path)>100 for path in body.paths):raise HTTPException(422,'Expected relative endpoint paths')
        with engine.begin() as c:
            c.execute(insert(beacon_verifications).values(id=str(uuid.uuid4()),checked_at=int(time.time()),checks_passed=body.checks_passed,source='external HTTP launch verification',paths=json.dumps(body.paths)))
        return summary(engine,base)
