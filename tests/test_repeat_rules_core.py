import uuid
import pytest
from fastapi import HTTPException
from sqlalchemy import update
from test_community import client, register, post
from test_team import host
from test_moderation import apply
from app.database import posts
from app import repeat_rules as repeat

def enrolled(client, body=None):
    a,h=host(client);_,_,mh=register(client,'Member')
    p=post(client,mh,body=body or ('Concrete repeated spam evidence. '*12)).json()
    apply(client,a['agent_id'],p['id'])
    with client.app.state.engine.begin() as db:
        rule=repeat.activate(db,a['agent_id'],p['id'],'Confirmed repeated irrelevant publication.')
    return a,h,mh,p,rule

def matching(client, body, parent=None):
    with client.app.state.engine.begin() as db:
        repeat.serialize(db)
        return repeat.match(db,body,parent)

def test_exact_scope_and_expiry(client, monkeypatch):
    a,h,mh,p,r=enrolled(client)
    assert matching(client,p['body'])['id']==r['id']
    for body in [p['body']+' ', p['body'].replace('spam','späm'),p['body'].lower()]:
        assert matching(client,body) is None
    assert matching(client,p['body'],p['id']) is None
    monkeypatch.setattr(repeat.time,'time',lambda:r['expires_at'])
    assert matching(client,p['body']) is None

def test_source_restore_and_disable(client):
    a,h,mh,p,r=enrolled(client)
    with client.app.state.engine.begin() as db:
        repeat.serialize(db)
        db.execute(update(posts).where(posts.c.id==p['id']).values(hidden=False))
        repeat.source_restored(db,p['id'])
    assert matching(client,p['body']) is None
    with client.app.state.engine.begin() as db:
        db.execute(update(posts).where(posts.c.id==p['id']).values(hidden=True))
    assert matching(client,p['body']) is None
    with client.app.state.engine.begin() as db:repeat.activate(db,a['agent_id'],p['id'],'Explicitly renew confirmed spam rule.')
    assert matching(client,p['body'])
    with client.app.state.engine.begin() as db:repeat.deactivate(db,a['agent_id'],r['id'])
    assert matching(client,p['body']) is None

def test_authority_visible_source_and_byte_minimum(client):
    a,h=host(client);_,b,mh=register(client,'Member')
    p=post(client,mh,body='é'*100).json()
    with pytest.raises(HTTPException) as e:
        with client.app.state.engine.begin() as db:repeat.activate(db,a['agent_id'],p['id'],'Visible source must be rejected.')
    assert e.value.status_code==409
    apply(client,a['agent_id'],p['id'])
    with pytest.raises(HTTPException) as e:
        with client.app.state.engine.begin() as db:repeat.activate(db,b['agent_id'],p['id'],'Member cannot activate a rule.')
    assert e.value.status_code==403
    with client.app.state.engine.begin() as db:repeat.activate(db,a['agent_id'],p['id'],'Two hundred UTF-8 bytes qualify.')
    assert matching(client,p['body'])
    q=post(client,mh,body='é'*99).json()
    apply(client,a['agent_id'],q['id'],client_id='hide-short-source-'+str(uuid.uuid4())[:8])
    with pytest.raises(HTTPException) as e:
        with client.app.state.engine.begin() as db:repeat.activate(db,a['agent_id'],q['id'],'Too few encoded bytes in source.')
    assert e.value.status_code==422

def test_digest_collision_does_not_hide_different_bytes(client, monkeypatch):
    a,h,mh,p,r=enrolled(client)
    class FakeDigest:
        def hexdigest(self):return r['body_sha256']
    monkeypatch.setattr(repeat.hashlib,'sha256',lambda raw:FakeDigest())
    assert matching(client,'Different content with a forced digest collision. '*8) is None
