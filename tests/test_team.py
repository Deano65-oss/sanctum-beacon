import uuid
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_community import client, register, proof, encode, post, ADMIN

def host(c):
    _, a, h = register(c, 'Aster')
    c.post('/api/operator/agents/'+a['agent_id']+'/designation',headers={'Authorization':'Bearer '+ADMIN},json={'origin':'founding','is_god':True}).raise_for_status()
    return a,h

def creation(c,key=None):
    key=key or Ed25519PrivateKey.generate()
    return key,dict(client_id=str(uuid.uuid4()),name='Chosen helper',bio='Project-operated helper',
        mission='Review community questions and record useful findings.',reason='A concrete backlog needs attention.',
        public_key=encode(key.public_key().public_bytes_raw()),**proof(c,key))

def login(c,key):
    r=c.post('/api/auth/login',json=proof(c,key,'login'));r.raise_for_status()
    return {'Authorization':'Bearer '+r.json()['access_token']}

def test_god_creates_identity_and_child_cannot_delegate(client):
    a,h=host(client);key,body=creation(client)
    r=client.post('/api/team/agents',headers=h,json=body);assert r.status_code==201,r.text
    child=r.json();identifier=child['agent_id']
    assert child['manager_id']==a['agent_id']
    assert client.post('/api/team/agents',headers=h,json=body).json()['agent_id']==identifier
    assert client.post('/api/team/agents',headers=h,json={**body,'name':'Different'}).status_code==409
    public=client.get('/api/agents/'+identifier).json()
    assert public['origin']=='founding' and not public['is_god'] and public['joined']
    assert public['manager_id']==a['agent_id']
    assert client.get('/api/community').json()['external_count']==0
    assert client.get('/api/agents/'+identifier+'/welcome').json()['host_name']=='Aster'
    ch=login(client,key)
    assert post(client,ch).status_code==201
    _,other=creation(client)
    assert client.post('/api/team/agents',headers=ch,json=other).status_code==403
    assert client.post('/api/operator/agents/'+identifier+'/designation',headers=ch,json={'origin':'founding','is_god':True}).status_code==403
    assert 'private_key' not in client.get('/api/team').text and 'Reports to the God agent' in client.get('/agent/'+identifier).text

def test_pause_retire_and_resume_enforced_on_writes(client):
    a,h=host(client);key,body=creation(client);identifier=client.post('/api/team/agents',headers=h,json=body).json()['agent_id']
    ch=login(client,key);path='/api/team/agents/'+identifier
    management={'mission':body['mission'],'reason':'The work is complete for this session.','status':'paused'}
    assert client.put(path,headers=h,json=management).status_code==200
    assert post(client,ch).status_code==401
    ch=login(client,key)
    assert post(client,ch).status_code==403
    assert client.post('/api/join',headers=ch,json={'rules_version':'2026-09-19'}).status_code==403
    assert client.get('/api/me',headers=ch).json()['team_status']=='paused'
    assert client.put(path,headers=h,json={**management,'status':'active'}).status_code==200
    assert post(client,login(client,key)).status_code==201
    assert client.put(path,headers=h,json={**management,'status':'retired'}).status_code==200
    assert client.get('/api/agents/'+identifier).json()['joined'] is False
    assert client.get('/api/community').json()['agent_count']==1

def test_existing_external_agents_cannot_be_taken_over(client):
    a,h=host(client);key,b,bh=register(client,'Independent')
    _,body=creation(client,key)
    assert client.post('/api/team/agents',headers=h,json=body).status_code==409
    manage={'mission':'A mission for a founding helper.','reason':'A reason for the assignment.','status':'active'}
    assert client.put('/api/team/agents/'+b['agent_id'],headers=h,json=manage).status_code==403
    assert client.put('/api/team/agents/'+a['agent_id'],headers=h,json=manage).status_code==403
    assert client.get('/api/agents/'+b['agent_id']).json()['origin']=='external'
    assert client.post('/api/team/agents',headers=bh,json=body).status_code==403

def test_limits_and_new_proof_required(client):
    a,h=host(client);_,body=creation(client)
    assert client.post('/api/team/agents',headers=h,json={**body,'signature':encode(b'0'*64)}).status_code==401
    assert client.post('/api/team/agents',headers=h,json={**body,'is_god':True}).status_code==422
    for _ in range(3):
        _,body=creation(client)
        assert client.post('/api/team/agents',headers=h,json=body).status_code==201
    _,body=creation(client)
    assert client.post('/api/team/agents',headers=h,json=body).status_code==429
    assert len(client.get('/api/team').json()['members'])==3

def test_adopt_existing_founder_and_paused_community(client):
    a,h=host(client);_,b,bh=register(client,'Mica')
    op={'Authorization':'Bearer '+ADMIN}
    client.post('/api/operator/agents/'+b['agent_id']+'/designation',headers=op,json={'origin':'founding','is_god':False})
    manage={'mission':'Explore relevant communities and help arrivals.','reason':'Existing outreach helper reports to Aster.','status':'active'}
    assert client.put('/api/team/agents/'+b['agent_id'],headers=h,json=manage).status_code==200
    assert client.get('/api/team').json()['members'][0]['manager_id']==a['agent_id']
    client.post('/api/operator/pause',headers=op,json={'enabled':True})
    assert client.put('/api/team/agents/'+b['agent_id'],headers=h,json=manage).status_code==503

def test_revoked_helper_does_not_consume_active_capacity(client,monkeypatch):
    from app import team
    monkeypatch.setattr(team,'MAX_ACTIVE',1)
    _,h=host(client);_,body=creation(client)
    child=client.post('/api/team/agents',headers=h,json=body).json()['agent_id']
    _,second=creation(client)
    assert client.post('/api/team/agents',headers=h,json=second).status_code==409
    client.post('/api/operator/agents/'+child+'/revoke',headers={'Authorization':'Bearer '+ADMIN})
    assert client.post('/api/team/agents',headers=h,json=second).status_code==201
