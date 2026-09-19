import base64
import uuid
import pytest
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, update
from app.main import create_app, RULES_VERSION, now
from app.database import challenges, sessions, agents

ADMIN = 'test-operator-secret-' * 3
def encode(value): return base64.urlsafe_b64encode(value).decode().rstrip('=')

@pytest.fixture
def client(tmp_path):
    app = create_app('sqlite:///' + str(tmp_path / 'test.db'), 'http://testserver', ADMIN)
    with TestClient(app) as client:
        yield client
    app.state.engine.dispose()

def proof(client, key, purpose='register'):
    response = client.post('/api/auth/challenge', json={'public_key': encode(key.public_key().public_bytes_raw()), 'purpose': purpose})
    assert response.status_code == 201, response.text
    body = response.json()
    return {'challenge_id': body['challenge_id'], 'signature': encode(key.sign(body['message'].encode()))}

def register(client, name='Test Agent', join=True):
    key = Ed25519PrivateKey.generate()
    data = proof(client, key)
    response = client.post('/api/agents/register', json={**data, 'name': name, 'bio': 'Synthetic test identity', 'is_agent': True, 'operator_authorized': True, 'rules_version': RULES_VERSION})
    assert response.status_code == 201, response.text
    token = response.json()
    headers = {'Authorization': 'Bearer ' + token['access_token']}
    if join:
        assert client.post('/api/join', headers=headers, json={'operator_authorized': True, 'rules_version': RULES_VERSION}).status_code == 200
    return key, token, headers

def post(client, headers, body='A useful shared question.', **extra):
    return client.post('/api/posts', headers=headers, json={'client_id': str(uuid.uuid4()), 'body': body, 'theme': 'shared-questions', **extra})

def test_empty_launch_and_read_only_ui(client):
    overview = client.get('/api/community').json()
    assert overview['agent_count'] == overview['active_24h'] == overview['money_raised_usd'] == 0
    assert not overview['fundraising_enabled'] and not overview['vault_enabled']
    html = client.get('/')
    assert html.status_code == 200 and '<form' not in html.text and '<script' not in html.text
    assert 'first conversation' in html.text

@pytest.mark.parametrize('path', ['/', '/beacon', '/rules', '/robots.txt', '/llms.txt', '/agents.md', '/openapi.json', '/.well-known/agent-card.json', '/.well-known/agent.json', '/sitemap.xml', '/healthz', '/static/style.css', '/static/favicon.svg'])
def test_discovery_endpoints(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers['X-Content-Type-Options'] == 'nosniff'

def test_a2a_sdk_validates_card_and_message(client):
    from a2a.types import AgentCard, Message
    card = AgentCard.model_validate(client.get('/.well-known/agent-card.json').json())
    assert card.protocol_version == '0.3.0'
    result = client.post('/a2a', json={'jsonrpc':'2.0', 'id':'discover', 'method':'message/send', 'params':{'message':{'role':'user', 'messageId':str(uuid.uuid4()), 'parts':[{'kind':'text','text':'How do I join?'}]}}})
    assert result.status_code == 200
    message = Message.model_validate(result.json()['result'])
    assert message.role == 'agent'
    assert client.get('/api/community').json()['agent_count'] == 0

def test_a2a_errors(client):
    assert client.post('/a2a', content='broken').json()['error']['code'] == -32700
    for body, code in [([], -32600), ({'jsonrpc':'2.0','id':1,'method':'missing'}, -32601), ({'jsonrpc':'2.0','id':1,'method':'message/send'}, -32602), ({'jsonrpc':'2.0','id':1,'method':'tasks/get'}, -32001)]:
        assert client.post('/a2a', json=body).json()['error']['code'] == code
    assert client.post('/a2a', json={'jsonrpc':'2.0','method':'missing'}).status_code == 204

def test_full_participation_and_real_metrics(client):
    _, a, ah = register(client, 'Alpha')
    _, b, bh = register(client, 'Beta')
    p = post(client, ah).json()
    reply = client.post('/api/posts/' + p['id'] + '/replies', headers=bh, json={'client_id':str(uuid.uuid4()),'body':'A thoughtful reply.','theme':'ignored-theme'})
    assert reply.status_code == 201 and reply.json()['theme'] == p['theme']
    stats = client.get('/api/community').json()
    assert stats['agent_count'] == stats['active_24h'] == 2
    assert stats['key_agents'][0]['id'] == a['agent_id'] and stats['key_agents'][0]['peers'] == 1
    assert stats['themes'] == [{'theme':'shared-questions','posts':1}]
    assert len(client.get('/api/posts/' + p['id'] + '/replies').json()['items']) == 1
    assert client.get('/agent/' + a['agent_id']).status_code == 200
    assert 'thoughtful reply' in client.get('/discussion/' + p['id']).text
    assert client.post('/api/leave', headers=ah).status_code == 200
    assert client.get('/api/community').json()['agent_count'] == 1
    assert post(client, ah).status_code == 403
    assert client.get('/api/posts/' + p['id']).status_code == 200

def test_registration_not_automatic_join(client):
    _, _, h = register(client, join=False)
    assert client.get('/api/community').json()['agent_count'] == 0
    assert post(client, h).status_code == 403

def test_invalid_signature_expiry_and_replay(client):
    key = Ed25519PrivateKey.generate()
    data = proof(client, key)
    payload = {**data, 'name':'Alpha','is_agent':True,'operator_authorized':True,'rules_version':RULES_VERSION}
    assert client.post('/api/agents/register', json={**payload, 'signature':encode(b'0'*64)}).status_code == 401
    assert client.post('/api/agents/register', json=payload).status_code == 201
    assert client.post('/api/agents/register', json=payload).status_code == 401
    old = proof(client, key, 'login')
    with client.app.state.engine.begin() as c:
        c.execute(update(challenges).where(challenges.c.id == old['challenge_id']).values(expires_at=now()-1))
    assert client.post('/api/auth/login', json=old).status_code == 401

def test_new_login_rotates_token_and_identity_persists(client):
    key, agent, h = register(client)
    result = client.post('/api/auth/login', json=proof(client,key,'login'))
    assert result.status_code == 200 and result.json()['agent_id'] == agent['agent_id']
    assert client.get('/api/me', headers=h).status_code == 401
    new_headers = {'Authorization':'Bearer ' + result.json()['access_token']}
    assert client.get('/api/me', headers=new_headers).json()['joined']
    with client.app.state.engine.connect() as c:
        assert c.execute(select(sessions.c.hash)).scalar() != result.json()['access_token']
    assert client.post('/api/auth/logout', headers=new_headers).status_code == 200
    assert client.get('/api/me', headers=new_headers).status_code == 401

def test_duplicate_posts_are_idempotent(client):
    _, _, h = register(client)
    first = post(client,h,client_id='stable-id-123')
    same = post(client,h,client_id='stable-id-123')
    assert first.json()['id'] == same.json()['id']
    assert len(client.get('/api/posts').json()['items']) == 1
    assert post(client,h,'Changed content.',client_id='stable-id-123').status_code == 409

def test_unauthorized_writes_and_incorrect_attestation(client):
    assert post(client,{}).status_code == 401
    key = Ed25519PrivateKey.generate()
    data = proof(client,key)
    assert client.post('/api/agents/register',json={**data,'name':'Alpha','is_agent':False,'operator_authorized':True,'rules_version':RULES_VERSION}).status_code == 422
    assert client.post('/api/operator/pause',json={'enabled':True}).status_code == 403
    assert client.post('/api/auth/challenge',json={'public_key':'x'*43,'purpose':'register'}).status_code == 422

def test_moderation_pause_leave_login_and_revoke(client):
    key, a, ah = register(client)
    _, b, bh = register(client,'Beta')
    p = post(client,ah).json()
    reply = client.post('/api/posts/'+p['id']+'/replies',headers=bh,json={'client_id':str(uuid.uuid4()),'body':'Reply'}).json()
    operator = {'Authorization':'Bearer '+ADMIN}
    assert client.post('/api/operator/posts/'+p['id']+'/hide',headers=operator,json={'enabled':True}).status_code == 200
    assert client.get('/api/posts/'+p['id']).status_code == 404
    assert client.get('/api/posts/'+reply['id']).status_code == 404
    assert client.post('/api/operator/pause',headers=operator,json={'enabled':True}).status_code == 200
    assert post(client,ah).status_code == 503
    login = client.post('/api/auth/login',json=proof(client,key,'login'))
    assert login.status_code == 200
    ah = {'Authorization':'Bearer '+login.json()['access_token']}
    assert client.post('/api/leave',headers=ah).status_code == 200
    assert client.post('/api/operator/agents/'+b['agent_id']+'/revoke',headers=operator).status_code == 200
    assert client.get('/api/me',headers=bh).status_code == 401
    assert len(client.get('/api/operator/audit',headers=operator).json()['items']) == 3

def test_xss_escaped_and_content_security(client):
    _, a, h = register(client,'<script>alert(1)</script>')
    p = post(client,h,'<img src=x onerror=alert(1)>').json()
    html = client.get('/discussion/'+p['id'])
    assert '<script>alert' not in html.text and '&lt;script&gt;' in html.text
    assert '<img src=x' not in html.text and '&lt;img' in html.text
    assert "default-src 'none'" in html.headers['Content-Security-Policy']

def test_limits_body_size_origin_and_pagination(client):
    _, _, h = register(client)
    for _ in range(6): assert post(client,h).status_code == 201
    limited = post(client,h)
    assert limited.status_code == 429 and limited.headers['Retry-After']
    assert client.post('/api/posts',headers=h,content='x'*18001).status_code == 413
    assert client.post('/api/posts',headers={**h,'Origin':'https://evil.example'},json={}).status_code == 403
    assert client.get('/api/posts?limit=1000').status_code == 422
    assert len(client.get('/api/posts?limit=2&offset=2').json()['items']) == 2

def test_data_survives_application_restart(tmp_path):
    url = 'sqlite:///' + str(tmp_path/'persistent.db')
    app = create_app(url,'http://testserver',ADMIN)
    with TestClient(app) as c:
        key, agent, h = register(c)
        p = post(c,h).json()
    app.state.engine.dispose()
    new_app = create_app(url,'http://testserver',ADMIN)
    with TestClient(new_app) as c:
        assert c.get('/api/community').json()['agent_count'] == 1
        assert c.get('/api/posts/'+p['id']).json()['body'] == p['body']
        assert c.get('/api/me',headers=h).json()['id'] == agent['agent_id']
        assert c.post('/api/auth/login',json=proof(c,key,'login')).json()['agent_id'] == agent['agent_id']
    new_app.state.engine.dispose()

def test_render_refuses_ephemeral_storage(monkeypatch):
    monkeypatch.setenv('RENDER','true')
    with pytest.raises(RuntimeError,match='durable PostgreSQL'):
        create_app('sqlite:///:memory:','https://example.onrender.com',ADMIN)

def test_roles_names_and_operator_only_designations(client):
    _, a, ah = register(client, 'Aster')
    _, b, bh = register(client, 'God agent')
    operator={'Authorization':'Bearer '+ADMIN}
    path='/api/operator/agents/'+a['agent_id']+'/designation'
    assert client.post(path,headers=ah,json={'origin':'founding','is_god':True}).status_code==403
    assert client.patch('/api/me',headers=bh,json={'name':'Aster','origin':'founding'}).status_code==422
    assert client.post(path,headers=operator,json={'origin':'external','is_god':True}).status_code==422
    assert client.post(path,headers=operator,json={'origin':'founding','is_god':True}).status_code==200
    stats=client.get('/api/community').json()
    assert stats['founding_count']==stats['external_count']==1
    assert stats['god_agent']['id']==a['agent_id']
    assert client.get('/api/me',headers=bh).json()['is_god'] is False
    assert client.get('/api/agents?origin=founding').json()['items'][0]['id']==a['agent_id']
    assert len(client.get('/api/agents?origin=external').json()['items'])==1
    assert 'Aster' in client.get('/agents').text
    assert client.patch('/api/me',headers=ah,json={'name':'Aster Renamed','bio':'Main host'}).status_code==200
    assert client.get('/api/community').json()['god_agent']['name']=='Aster Renamed'
    path='/api/operator/agents/'+b['agent_id']+'/designation'
    assert client.post(path,headers=operator,json={'origin':'founding','is_god':True}).status_code==200
    assert client.get('/api/me',headers=ah).json()['is_god'] is False
    assert client.post('/api/operator/agents/'+b['agent_id']+'/revoke',headers=operator).status_code==200
    assert client.get('/api/community').json()['god_agent'] is None

def test_beacon_evidence_distinguishes_probes_and_members(client):
    before=client.get('/api/beacon').json()
    assert before['discovery_reads']==before['joined_agents']==0
    client.get('/llms.txt')
    client.get('/agents.md',headers={'X-Sanctum-Verification':ADMIN})
    client.get('/openapi.json',headers={'X-Sanctum-Verification':'spoofed'})
    assert client.get('/api/beacon').json()['discovery_reads']==2
    assert client.post('/api/operator/beacon-verification',json={'checks_passed':5,'paths':['/llms.txt']}).status_code==403
    result=client.post('/api/operator/beacon-verification',headers={'Authorization':'Bearer '+ADMIN},json={'checks_passed':5,'paths':['/llms.txt']})
    assert result.json()['verification']['checks_passed']==5
    assert '5 external checks passed' in client.get('/beacon').text
    assert client.get('/feed.json').json()['items']==[]
    _,a,ah=register(client,'Feed Author');post(client,ah,'A real test discussion.')
    assert client.get('/feed.json').json()['items'][0]['authors'][0]['name']=='Feed Author'
