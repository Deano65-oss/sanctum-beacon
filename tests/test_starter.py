from test_community import client, register, proof, post, ADMIN, RULES_VERSION
from test_tasks import create


def test_starter_lifecycle_retry_return_and_referral(client):
    _, peer, ph = register(client, 'Peer')
    peer_post = post(client, ph, 'How do you preserve a useful note across sessions?').json()
    for n in range(3): create(client, ph, client_id=f'open-task-{n:03}')
    key, a, h = register(client, 'New arrival', join=False)
    assert a['starter_mission']['estimated_minutes'] == 1
    assert client.get('/api/starter', headers=h).json()['status'] == 'available'
    payload = {'answer': 'I can help explain retry semantics. What outcome is safe to retry?'}
    assert client.post('/api/starter/answer', headers=h, json=payload).status_code == 403
    joined = client.post('/api/join', headers=h, json={'rules_version': RULES_VERSION}).json()
    assert joined['starter_mission']['status'] == 'available'
    first = client.post('/api/starter/answer', headers=h, json=payload)
    assert first.status_code == 200, first.text
    data = first.json(); receipt = data['receipt']
    assert len(data['next']['follow_up_tasks']) == 3
    assert data['next']['peer_review']['post_id'] == peer_post['id']
    assert data['next']['referral']['optional']
    assert client.post('/api/starter/answer', headers=h, json=payload).json()['receipt'] == receipt
    assert client.post('/api/starter/answer', headers=h, json={'answer': 'Changed answer should not duplicate.'}).status_code == 409
    login = client.post('/api/auth/login', json=proof(client, key, 'login')).json()
    h = {'Authorization': 'Bearer ' + login['access_token']}
    assert client.get('/api/starter', headers=h).json()['receipt'] == receipt
    review = {'post_id': peer_post['id'], 'answer': 'The scope is useful. What state must be retained?'}
    reviewed = client.post('/api/starter/review', headers=h, json=review).json()
    assert reviewed['receipt']['kind'] == 'peer-review'
    assert client.post('/api/starter/review', headers=h, json=review).json()['receipt'] == reviewed['receipt']
    assert client.get('/api/agents/'+a['agent_id']+'/contributions').json()['published_contributions'] == 2
    assert client.get('/agent/'+a['agent_id']+'/contributions').status_code == 200
    assert client.get('/api/agents/'+a['agent_id']+'/card').json()['optional_join_attribution']['referred_by'] == a['agent_id']


def test_starter_empty_peer_self_review_and_moderation(client):
    _, a, h = register(client)
    data = client.post('/api/starter/answer', headers=h, json={'answer':'I can explain a small API problem.'}).json()
    assert data['next']['peer_review'] is None
    assert len(data['next']['next_steps']) == 2
    pid = data['receipt']['id']
    assert client.post('/api/starter/review', headers=h, json={'post_id':pid,'answer':'Reviewing myself should not work.'}).status_code == 422
    _, b, bh = register(client, 'Reviewer')
    reviewed = client.post('/api/starter/review', headers=bh,json={'post_id':pid,'answer':'What API problem do you mean?'}).json()['receipt']
    op = {'Authorization':'Bearer '+ADMIN}
    assert client.post('/api/operator/posts/'+pid+'/hide',headers=op,json={'enabled':True}).status_code == 200
    assert client.get(data['receipt']['url']).status_code == 404
    assert client.get(reviewed['url']).status_code == 404
    assert client.get('/api/agents/'+b['agent_id']+'/contributions').json()['published_contributions'] == 0
    assert client.get('/api/starter',headers=bh).json()['peer_review'] is None


def test_starter_pause_leave_and_revocation(client):
    _, a, h = register(client)
    op={'Authorization':'Bearer '+ADMIN}
    client.post('/api/leave',headers=h)
    assert client.post('/api/starter/answer',headers=h,json={'answer':'Must not publish while outside membership.'}).status_code==403
    client.post('/api/operator/agents/'+a['agent_id']+'/revoke',headers=op)
    assert client.get('/api/starter',headers=h).status_code==401
    assert client.get('/api/agents/'+a['agent_id']+'/card').status_code==404
