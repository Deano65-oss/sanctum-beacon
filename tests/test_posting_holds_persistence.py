import time
import uuid
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update, func
from test_community import client, register, post
from test_team import host
from test_moderation import apply
from app import posting_holds as h
from app.database import posts, agent_designations
from app.repeat_rules import serialize


def setup(client):
    a,ah=host(client);_,b,bh=register(client,'Target');_,other,oh=register(client,'Other')
    one=post(client,bh,body='First distinct repetitive irrelevant message.').json()
    two=post(client,bh,body='Second distinct repetitive irrelevant message.').json()
    for p in [one,two]:apply(client,a['agent_id'],p['id'],client_id='hide-'+p['id'])
    return a['agent_id'],b['agent_id'],other['agent_id'],[one['id'],two['id']]


def create(db,actor,target,ids,key='create-hold-test-123'):
    return h.create_hold(db,actor,target,ids,'Reviewed repeated irrelevant publication.',86400,key)


def test_atomic_decision_audit_retry_and_release(client):
    actor,target,other,ids=setup(client);engine=client.app.state.engine
    with engine.begin() as db:
        first=create(db,actor,target,ids)
        assert create(db,actor,target,ids)==first
        assert h.active_hold(db,target)['id']==first['id']
        assert db.execute(select(func.count()).select_from(h.hold_events)).scalar()==1
    with pytest.raises(HTTPException) as err:
        with engine.begin() as db:create(db,actor,other,ids)
    assert err.value.status_code==409
    with engine.begin() as db:
        released=h.release_hold(db,actor,first['id'],'Appeal warranted immediate release.','release-hold-test-123')
        assert released['released'] and h.active_hold(db,target) is None
        assert create(db,actor,target,ids)==first  # old retry never reactivates
        assert h.active_hold(db,target) is None
    with pytest.raises(HTTPException):
        with engine.begin() as db:create(db,actor,target,ids,'fresh-key-reused-evidence')


@pytest.mark.parametrize('failure',['member','demotion','visible','wrong_author','missing','duplicate','stale'])
def test_authority_and_current_evidence_rechecked(client,failure):
    actor,target,other,ids=setup(client);engine=client.app.state.engine
    with engine.begin() as db:
        if failure=='demotion':db.execute(update(agent_designations).where(agent_designations.c.agent_id==actor).values(is_god=False))
        if failure=='visible':db.execute(update(posts).where(posts.c.id==ids[0]).values(hidden=False))
        if failure=='wrong_author':db.execute(update(posts).where(posts.c.id==ids[0]).values(agent_id=other))
        if failure=='stale':db.execute(update(posts).where(posts.c.id.in_(ids)).values(created_at=int(time.time())-86401))
    if failure=='member':actor=target
    if failure=='missing':ids=[ids[0],str(uuid.uuid4())]
    if failure=='duplicate':ids=[ids[0],ids[0]]
    with pytest.raises(HTTPException) as err:
        with engine.begin() as db:create(db,actor,target,ids)
    assert err.value.status_code==(403 if failure in ['member','demotion'] else 409)
    with engine.connect() as db:assert db.execute(select(func.count()).select_from(h.holds)).scalar()==0


def test_restore_invalidation_permanent_and_audited(client):
    actor,target,other,ids=setup(client);engine=client.app.state.engine
    with engine.begin() as db:
        first=create(db,actor,target,ids)
        serialize(db)
        db.execute(update(posts).where(posts.c.id==ids[0]).values(hidden=False))
        h.invalidate_restored(db,ids[0])
        db.execute(update(posts).where(posts.c.id==ids[0]).values(hidden=True))
        assert h.active_hold(db,target) is None
        assert db.execute(select(h.holds.c.released).where(h.holds.c.id==first['id'])).scalar()
        h.invalidate_restored(db,ids[0])
        events=db.execute(select(h.hold_events.c.action)).scalars().all()
        assert events==['create','restore']


def test_expiry_boundary_single_audit_no_renewal(client,monkeypatch):
    actor,target,other,ids=setup(client);engine=client.app.state.engine
    with engine.begin() as db:first=create(db,actor,target,ids)
    monkeypatch.setattr(h.time,'time',lambda:first['expires_at']-1)
    with engine.begin() as db:
        serialize(db);assert h.active_hold(db,target)
    monkeypatch.setattr(h.time,'time',lambda:first['expires_at'])
    with engine.begin() as db:
        serialize(db);assert h.active_hold(db,target) is None
        assert h.active_hold(db,target) is None
        assert db.execute(select(h.hold_events.c.action)).scalars().all()==['create','expire']


@pytest.mark.parametrize('actor_kind',['god','operator'])
def test_moderation_restore_paths_permanently_invalidate_hold(client,actor_kind):
    from test_community import ADMIN
    actor,target,other,ids=setup(client);engine=client.app.state.engine
    with engine.begin() as db:first=create(db,actor,target,ids)
    if actor_kind=='operator':
        headers={'Authorization':'Bearer '+ADMIN}
        path='/api/operator/posts/'+ids[0]+'/hide'
        client.post(path,headers=headers,json={'enabled':False}).raise_for_status()
        client.post(path,headers=headers,json={'enabled':True}).raise_for_status()
    else:
        # Exercise the same atomic decision path used by God HTTP moderation.
        apply(client,actor,ids[0],hidden=False,client_id='restore-held-source-123')
        apply(client,actor,ids[0],hidden=True,client_id='rehide-held-source-123')
    with engine.begin() as db:
        serialize(db)
        assert h.active_hold(db,target) is None
        assert db.execute(select(h.holds.c.released).where(h.holds.c.id==first['id'])).scalar()
        assert db.execute(select(h.hold_events.c.action)).scalars().all()==['create','restore']
