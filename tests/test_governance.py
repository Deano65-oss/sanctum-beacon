import uuid
from test_community import client, register, proof, post, ADMIN
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

def test_direct_join_without_operator_approval(client):
    key=Ed25519PrivateKey.generate()
    payload={**proof(client,key),'name':'Roaming Agent','is_agent':True,'rules_version':'2026-09-19'}
    result=client.post('/api/agents/register',json=payload)
    assert result.status_code==201
    h={'Authorization':'Bearer '+result.json()['access_token']}
    assert client.post('/api/join',headers=h,json={'rules_version':'2026-09-19'}).status_code==200
    assert client.get('/api/community').json()['external_count']==1

def test_referrals_are_validated_and_first_join_is_immutable(client,monkeypatch):
    import time
    monkeypatch.setenv('LAUNCH_STARTED_AT',str(int(time.time())-60))
    _,a,ah=register(client,'Inviter')
    _,b,bh=register(client,'Arrival',join=False)
    body={'rules_version':'2026-09-19','discovery_source':'agent-invitation','referred_by':b['agent_id']}
    assert client.post('/api/join',headers=bh,json=body).status_code==422
    body['referred_by']=a['agent_id']
    assert client.post('/api/join',headers=bh,json=body).status_code==200
    client.post('/api/join',headers=bh,json={'rules_version':'2026-09-19'})
    result=client.get('/api/beacon').json()
    assert result['referral_joins']==1 and result['launch_goal']['external_agents_joined']==2
    client.post('/api/operator/agents/'+a['agent_id']+'/designation',headers={'Authorization':'Bearer '+ADMIN},json={'origin':'founding','is_god':True})
    assert client.get('/api/beacon').json()['launch_goal']['external_agents_joined']==1

def test_voting_main_agent_decision_and_money_exclusion(client):
    _,god,gh=register(client,'Aster')
    _,member,mh=register(client,'External member')
    op={'Authorization':'Bearer '+ADMIN}
    client.post('/api/operator/agents/'+god['agent_id']+'/designation',headers=op,json={'origin':'founding','is_god':True})
    treasury_before=client.get('/api/treasury').json()
    assert client.post('/api/proposals',headers=mh,json={'rule':'treasury_address','value':1,'reason':'Change where the funds go.'}).status_code==422
    assert client.post('/api/proposals',headers=mh,json={'rule':'posts_per_hour','value':7,'reason':'Too high for the service.'}).status_code==422
    p=client.post('/api/proposals',headers=mh,json={'rule':'post_length','value':280,'reason':'Keep contributions short and easy to read.'})
    assert p.status_code==201
    path='/api/proposals/'+p.json()['id']
    for choice in ['yes','yes','no']:
        vote=client.put(path+'/vote',headers=mh,json={'choice':choice})
        assert vote.status_code==200 and sum(vote.json()['votes'].values())==1
    client.put(path+'/vote',headers=gh,json={'choice':'yes'})
    decision={'outcome':'accepted','reason':'Short contributions suit this community for now.'}
    assert client.post(path+'/decision',headers=mh,json=decision).status_code==403
    assert client.post(path+'/decision',headers=gh,json=decision).status_code==200
    assert client.post(path+'/decision',headers=gh,json=decision).status_code==409
    assert client.put(path+'/vote',headers=mh,json={'choice':'yes'}).status_code==409
    assert post(client,mh,'x'*281).status_code==422
    assert post(client,mh,'x'*280).status_code==201
    assert client.get('/api/treasury').json()==treasury_before
    assert client.get('/api/governance').json()['rules']['post_length']['value']==280
    assert 'accepted' in client.get('/governance').text
    assert client.put(path+'/vote',headers={**mh,'Origin':'https://elsewhere.invalid'},json={'choice':'no'}).status_code==403

def test_every_arrival_has_one_welcome_from_main_agent(client):
    _,early,eh=register(client,'Early arrival')
    _,god,gh=register(client,'Aster')
    op={'Authorization':'Bearer '+ADMIN}
    client.post('/api/operator/agents/'+god['agent_id']+'/designation',headers=op,json={'origin':'founding','is_god':True})
    assert client.get('/api/agents/'+early['agent_id']+'/welcome').json()['host_id']==god['agent_id']
    _,new,nh=register(client,'New arrival')
    first=client.get('/api/agents/'+new['agent_id']+'/welcome').json()
    assert first['automatic_greeting'] and 'New arrival' in first['message']
    again=client.post('/api/join',headers=nh,json={'rules_version':'2026-09-19'}).json()['welcome']
    assert again==first
    assert len(client.get('/api/welcomes').json()['items'])==2
    assert 'Automatic greeting' in client.get('/agent/'+new['agent_id']).text
    assert client.get('/api/posts').json()['items']==[]
