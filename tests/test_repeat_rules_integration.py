import uuid
from concurrent.futures import ThreadPoolExecutor
from test_community import client, register, post, ADMIN
from test_team import host
from test_moderation import route
from test_mcp import call

def setup(client):
    a,h=host(client);_,_,mh=register(client,'Source');_,_,nh=register(client,'Retry author')
    p=post(client,mh,body='Precisely repeated irrelevant long message. '*10).json()
    route(client,h,p['id']).raise_for_status()
    payload={'client_id':'activate-repeat-rule-123','enabled':True,'reason':'Confirmed exact repeat of retained spam.'}
    api='/api/moderation/repeat-rules/'+p['id']
    result=client.post(api,headers=h,json=payload);result.raise_for_status()
    return h,mh,nh,p,api,payload,result.json()

def test_atomic_hold_public_reason_restore_and_retry(client):
    h,mh,nh,p,api,payload,rule=setup(client)
    key=str(uuid.uuid4());first=post(client,nh,body=p['body'],client_id=key).json()
    assert first['hidden'] and first['publication_status']=='held' and first['appeal_path']
    assert rule['rule']['id'] in first['moderation']['reason']
    assert first['moderation']['latest_action']['actor_kind']=='system'
    assert client.get('/api/posts/'+first['id']).status_code==404
    assert client.get('/discussion/'+first['id']).status_code==200
    assert client.get('/api/moderation/posts/'+first['id'],headers=h).json()['body']==p['body']
    assert post(client,nh,body=p['body'],client_id=key).json()['id']==first['id']
    route(client,h,first['id'],key='restore-held-repeat-123',hidden=False).raise_for_status()
    retry=post(client,nh,body=p['body'],client_id=key).json()
    assert not retry['hidden'] and retry['publication_status']=='published'
    assert 'actor_kind' in client.get('/api/moderation/events').text

def test_rule_retry_cannot_reactivate_after_source_restore(client):
    h,mh,nh,p,api,payload,rule=setup(client)
    assert client.post(api,headers=h,json=payload).json()==rule
    assert len(client.get('/api/moderation/repeat-rule-events').json()['items'])==1
    assert client.post(api,headers=h,json={**payload,'reason':'Conflicting reused activation key.'}).status_code==409
    client.post('/api/operator/posts/'+p['id']+'/hide',headers={'Authorization':'Bearer '+ADMIN},json={'enabled':False}).raise_for_status()
    assert not client.get('/api/moderation/repeat-rules').json()['items'][0]['active']
    assert client.post(api,headers=h,json=payload).json()==rule
    assert not post(client,nh,body=p['body']).json()['hidden']

def test_permissions_mcp_author_notice_and_nonmatches(client):
    h,mh,nh,p,api,payload,rule=setup(client)
    assert client.post(api,headers=mh,json={**payload,'client_id':'member-activation-123'}).status_code==403
    assert client.post(api,json=payload).status_code==401
    held=call(client,'contribute',{'body':p['body'],'client_id':str(uuid.uuid4())},nh)
    assert not held.get('isError')
    receipt=held['structuredContent']['data']
    assert receipt['hidden'] and receipt['publication_status']=='held' and receipt['appeal_path']
    visible=post(client,nh,body=p['body']+' altered').json()
    assert not visible['hidden']
    reply=client.post('/api/posts/'+visible['id']+'/replies',headers=nh,json={'body':p['body'],'client_id':str(uuid.uuid4())}).json()
    assert not reply['hidden']

def test_concurrent_publication_one_retained_hold_and_event(client):
    h,mh,nh,p,api,payload,rule=setup(client)
    key=str(uuid.uuid4())
    with ThreadPoolExecutor(2) as pool:
        responses=list(pool.map(lambda _:post(client,nh,body=p['body'],client_id=key),range(2)))
    assert [x.status_code for x in responses]==[201,201]
    rows=[x.json() for x in responses];assert rows[0]['id']==rows[1]['id'] and all(x['hidden'] for x in rows)
    events=client.get('/api/moderation/events').json()['items']
    assert len([x for x in events if x['post_id']==rows[0]['id']])==1


def test_held_starter_receipt_is_author_only_and_recoverable(client):
    h,mh,nh,p,api,payload,rule=setup(client)
    first=client.post('/api/starter/answer',headers=nh,json={'answer':p['body']})
    first.raise_for_status();receipt=first.json()['receipt']
    assert receipt['hidden'] and receipt['publication_status']=='held' and receipt['appeal_path']
    assert first.json()['next']['status']=='held'
    assert client.get('/api/starter',headers=nh).json()['receipt']['id']==receipt['id']
    assert client.get('/api/starter',headers=mh).json()['receipt'] is None
    assert client.get('/api/receipts/'+receipt['id']).status_code==404
    retry=call(client,'starter_answer',{'answer':p['body']},nh)['structuredContent']['data']['receipt']
    assert retry['id']==receipt['id'] and retry['hidden']

def test_automatic_disable_audit_and_rule_decision_concurrency(client):
    h,mh,nh,p,api,payload,rule=setup(client)
    with ThreadPoolExecutor(2) as pool:
        replies=list(pool.map(lambda _:client.post(api,headers=h,json=payload),range(2)))
    assert all(x.status_code==200 for x in replies)
    assert len(client.get('/api/moderation/repeat-rule-events').json()['items'])==1
    route(client,h,p['id'],key='restore-source-audit-123',hidden=False).raise_for_status()
    audit=client.get('/api/moderation/repeat-rule-events').json()['items']
    assert len(audit)==2 and audit[-1]['actor_kind']=='system' and not audit[-1]['enabled']
    assert audit[-1]['source_post_id']==p['id']
