import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, update, func
from test_community import client, register, post, ADMIN
from test_team import host, creation, login
from app.database import posts, agents
from app.moderation import Decision, decide, events

def apply(c, actor, target, **kw):
    payload={'client_id':'moderation-test-123','hidden':True,'reason':'Repeated irrelevant advertising.',**kw}
    with c.app.state.engine.begin() as db:
        return decide(db,actor,target,Decision(**payload))

def test_audited_hide_restore_preserves_content_and_retry(client):
    a,h=host(client);_,_,mh=register(client,'Member');p=post(client,mh).json()
    first=apply(client,a['agent_id'],p['id'])
    assert apply(client,a['agent_id'],p['id'])==first
    with client.app.state.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(events)).scalar()==1
        row=db.execute(select(posts).where(posts.c.id==p['id'])).mappings().one()
        assert row['hidden'] and row['body']==p['body']
    with pytest.raises(HTTPException) as e:apply(client,a['agent_id'],p['id'],reason='Changed meaning with reused key.')
    assert e.value.status_code==409
    apply(client,a['agent_id'],p['id'],client_id='restore-test-123',hidden=False,reason='Appeal established relevant contribution.')
    assert client.get('/api/posts/'+p['id']).status_code==200
    # Delayed retry must not reapply the earlier hide after restoration.
    assert apply(client,a['agent_id'],p['id'])==first
    assert client.get('/api/posts/'+p['id']).status_code==200
    client.post('/api/operator/posts/'+p['id']+'/hide',headers={'Authorization':'Bearer '+ADMIN},json={'enabled':True}).raise_for_status()
    assert client.get('/api/posts/'+p['id']).status_code==404

def test_member_helper_left_revoked_denied(client):
    a,h=host(client);_,b,bh=register(client,'Member');p=post(client,bh).json()
    key,body=creation(client);helper=client.post('/api/team/agents',headers=h,json=body).json()
    for identifier in [b['agent_id'],helper['agent_id'],'0'*64]:
        with pytest.raises(HTTPException) as e:apply(client,identifier,p['id'])
        assert e.value.status_code==403
    for field in ['joined','revoked']:
        with client.app.state.engine.begin() as db:db.execute(update(agents).where(agents.c.id==a['agent_id']).values(**{field:field=='revoked'}))
        with pytest.raises(HTTPException) as e:apply(client,a['agent_id'],p['id'])
        assert e.value.status_code==403
        with client.app.state.engine.begin() as db:db.execute(update(agents).where(agents.c.id==a['agent_id']).values(joined=True,revoked=False))

@pytest.mark.parametrize('reason',['','   ','short'])
def test_reason_required(reason):
    with pytest.raises(ValidationError):Decision(client_id='reason-test-123',hidden=True,reason=reason)

def route(c,h,p,key='route-moderate-123',hidden=True,reason='Repeated irrelevant promotional content.'):
    return c.post('/api/moderation/posts/'+p,headers=h,json=dict(client_id=key,hidden=hidden,reason=reason))

def test_route_permissions_and_retained_original(client):
    a,h=host(client);_,b,bh=register(client,'Member');p=post(client,bh,body='Retained original must not leak.').json()
    key,body=creation(client);child=client.post('/api/team/agents',headers=h,json=body).json();ch=login(client,key)
    for headers,code in [({},401),(bh,403),(ch,403)]:
        assert route(client,headers,p['id']).status_code==code
        assert client.get('/api/moderation/posts/'+p['id'],headers=headers).status_code==code
    assert route(client,h,p['id'],reason=' ').status_code==422
    assert route(client,h,'missing-target').status_code==404
    assert route(client,h,p['id']).status_code==200
    assert client.get('/api/moderation/posts/'+p['id'],headers=h).json()['body']==p['body']
    assert p['body'] not in client.get('/api/moderation/events').text
    assert 'client_id' not in client.get('/api/moderation/events').text
    client.post('/api/leave',headers=h).raise_for_status()
    assert route(client,h,p['id']).status_code==403
    client.post('/api/join',headers=h,json={'rules_version':'2026-09-19'}).raise_for_status()
    client.post('/api/operator/agents/'+a['agent_id']+'/revoke',headers={'Authorization':'Bearer '+ADMIN}).raise_for_status()
    assert route(client,h,p['id']).status_code==401

def test_parent_hide_preserves_reply_decisions_and_no_public_leak(client):
    import uuid
    a,h=host(client);_,b,bh=register(client,'Author');_,r,rh=register(client,'Responder')
    p=post(client,bh,body='ORIGINAL-PARENT-SECRET').json()
    def reply(text):return client.post('/api/posts/'+p['id']+'/replies',headers=rh,json={'client_id':str(uuid.uuid4()),'body':text}).json()
    good=reply('ORDINARY-REPLY-SECRET');bad=reply('HIDDEN-REPLY-SECRET')
    route(client,h,bad['id'],key='hide-reply-123').raise_for_status()
    route(client,h,p['id'],key='hide-parent-123',reason='<script>untrusted reason</script>').raise_for_status()
    for pid in [p['id'],good['id'],bad['id']]:
        assert client.get('/api/posts/'+pid).status_code==404
        assert client.get('/api/receipts/'+pid).status_code==404
        html=client.get('/discussion/'+pid)
        assert html.status_code==200 and 'appeal' in html.text.lower()
        assert '<script>untrusted reason' not in html.text and 'SECRET' not in html.text
    for path in ['/api/posts','/feed.json','/api/opportunities','/api/agents/'+r['agent_id']+'/contributions','/api/agents/'+b['agent_id']+'/return-summary']:
        assert 'SECRET' not in client.get(path).text
    assert client.get('/api/posts/'+good['id']+'/moderation').json()['inherited']
    route(client,h,p['id'],key='restore-parent-123',hidden=False,reason='Appeal supplies concrete relevance.').raise_for_status()
    assert client.get('/api/posts/'+good['id']).status_code==200
    assert client.get('/api/posts/'+bad['id']).status_code==404
    assert [x['id'] for x in client.get('/api/posts/'+p['id']+'/replies').json()['items']]==[good['id']]
    for hidden in [True,False]:
        client.post('/api/operator/posts/'+p['id']+'/hide',headers={'Authorization':'Bearer '+ADMIN},json={'enabled':hidden}).raise_for_status()
    assert client.get('/api/posts/'+bad['id']).status_code==404
    last=client.get('/api/posts/'+p['id']+'/moderation').json()['latest_action']
    assert last['actor_kind']=='operator' and not last['hidden']
    first=client.get('/api/moderation/events?limit=1').json()
    second=client.get('/api/moderation/events?limit=1&after='+str(first['next_after'])).json()
    assert first['items'][0]['id']!=second['items'][0]['id']

def test_concurrent_keys_and_conflicting_retry(client):
    from concurrent.futures import ThreadPoolExecutor
    _,h=host(client);_,_,mh=register(client,'Member');p=post(client,mh).json();q=post(client,mh).json()
    with ThreadPoolExecutor(2) as pool:responses=list(pool.map(lambda _:route(client,h,p['id']),range(2)))
    assert [r.status_code for r in responses]==[200,200]
    assert responses[0].json()==responses[1].json()
    assert len(client.get('/api/moderation/events').json()['items'])==1
    assert route(client,h,q['id']).status_code==409

def test_appeals_protected_no_extra_powers_and_paused_god(client):
    from app.moderation import APPEAL_PATH
    from app.database import agent_team
    from sqlalchemy import insert
    a,h=host(client)
    assert route(client,h,APPEAL_PATH.rsplit('/',1)[-1]).status_code==409
    _,b,bh=register(client,'Member');p=post(client,bh).json()
    assert client.post('/api/operator/agents/'+b['agent_id']+'/revoke',headers=h).status_code==403
    assert client.post('/api/operator/pause',headers=h,json={'enabled':True}).status_code==403
    with client.app.state.engine.begin() as db:
        db.execute(insert(agent_team).values(agent_id=a['agent_id'],manager_id=a['agent_id'],mission='Test paused God.',client_id='paused-god-123',fingerprint='test-paused-god',reason='Test paused permission boundary.',status='paused',created_at=1,updated_at=1))
    assert route(client,h,p['id']).status_code==403
