import pytest
from test_community import client,register,post,ADMIN
from test_team import host
from test_moderation import route
from app import posting_holds as h

def setup(c):
    a,ah=host(c);_,b,bh=register(c,'Held');ids=[]
    for text in ['First distinct reviewed irrelevant post.','Second distinct reviewed irrelevant post.']:
        p=post(c,bh,body=text).json();ids.append(p['id'])
        route(c,ah,p['id'],key='hide-'+p['id']).raise_for_status()
    return a,ah,b,bh,ids

def decision(ids,**kw):
    return dict(client_id='create-route-123',evidence_ids=ids,
        reason='Reviewed repeat spam from this one account.',duration=86400,**kw)

def test_routes_authority_public_audit_and_operator_release(client):
    a,ah,b,bh,ids=setup(client);path='/api/moderation/accounts/'+b['agent_id']+'/posting-holds'
    for headers,code in [({},401),(bh,403)]:
        assert client.post(path,headers=headers,json=decision(ids)).status_code==code
    first=client.post(path,headers=ah,json=decision(ids));first.raise_for_status();hold=first.json()
    assert client.post(path,headers=ah,json=decision(ids)).json()==hold
    assert client.get('/api/agents/'+b['agent_id']+'/posting-hold').json()['active_hold']==hold
    events=client.get('/api/moderation/posting-hold-events').json()['items']
    assert len(events)==1 and events[0]['actor_kind']=='god'
    assert 'fingerprint' not in events[0] and 'client_id' not in events[0]
    release='/api/operator/posting-holds/'+hold['id']+'/release'
    payload={'client_id':'operator-release-123','reason':'Immediate appeal release by the operator.'}
    assert client.post(release,headers=bh,json=payload).status_code==403
    owner={'Authorization':'Bearer '+ADMIN}
    released=client.post(release,headers=owner,json=payload);released.raise_for_status()
    assert released.json()['released']
    assert client.post(release,headers=owner,json=payload).json()==released.json()
    assert client.get('/api/agents/'+b['agent_id']+'/posting-hold').json()['active_hold'] is None
    assert client.post(path,headers=ah,json=decision(ids)).json()==hold
    assert client.get('/api/agents/'+b['agent_id']+'/posting-hold').json()['active_hold'] is None
    events=client.get('/api/moderation/posting-hold-events').json()['items']
    assert len(events)==2 and events[1]['actor_kind']=='operator'

@pytest.mark.parametrize('duration',[True,0,86401,'3600'])
def test_strict_bounded_duration(client,duration):
    a,ah,b,bh,ids=setup(client);body=decision(ids);body['duration']=duration
    assert client.post('/api/moderation/accounts/'+b['agent_id']+'/posting-holds',headers=ah,json=body).status_code==422

def test_public_expiry_commits_once_and_god_release(client,monkeypatch):
    a,ah,b,bh,ids=setup(client);path='/api/moderation/accounts/'+b['agent_id']+'/posting-holds'
    body=decision(ids);body['duration']=1
    held=client.post(path,headers=ah,json=body).json();monkeypatch.setattr(h.time,'time',lambda:held['expires_at'])
    status='/api/agents/'+b['agent_id']+'/posting-hold'
    assert client.get(status).json()['active_hold'] is None
    assert client.get(status).json()['active_hold'] is None
    events=client.get('/api/moderation/posting-hold-events').json()['items']
    assert [e['action'] for e in events]==['create','expire'] and events[-1]['actor_kind']=='system'
    release='/api/moderation/posting-holds/'+held['id']+'/release'
    payload={'client_id':'god-release-test-123','reason':'God confirms release following appeal.'}
    assert client.post(release,headers=bh,json=payload).status_code==403
    client.post(release,headers=ah,json=payload).raise_for_status()
    assert client.get('/api/moderation/posting-hold-events').json()['items'][-1]['actor_kind']=='god'
