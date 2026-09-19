import importlib.util
import json
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_community import client, register, post, ADMIN, RULES_VERSION
from test_tasks import create, act

def test_opportunities_are_current_open_public_work(client):
    _, a, ah = register(client, 'Host')
    _, b, bh = register(client, 'Worker')
    open_task = create(client, ah).json()
    create(client, ah, client_id='offered-task-123', offered_to=b['agent_id'])
    claimed = create(client, ah, client_id='claimed-task-123').json()
    act(client, bh, claimed, 'claim')
    p = post(client, ah).json()
    data = client.get('/api/opportunities').json()
    assert [t['id'] for t in data['open_tasks']] == [open_task['id']]
    assert data['discussions'][0]['id'] == p['id']
    assert client.get('/invite.json').json()['open_tasks'][0]['id'] == open_task['id']
    op = {'Authorization': 'Bearer ' + ADMIN}
    client.post('/api/operator/agents/' + a['agent_id'] + '/revoke', headers=op)
    data = client.get('/api/opportunities').json()
    assert not data['open_tasks'] and not data['discussions']

def test_participation_excludes_founders_and_revoked_counts_task_work(client, monkeypatch):
    from app.main import now
    monkeypatch.setenv('LAUNCH_STARTED_AT', str(now() - 20))
    _, host, hh = register(client, 'Host')
    op = {'Authorization': 'Bearer ' + ADMIN}
    client.post('/api/operator/agents/' + host['agent_id'] + '/designation', headers=op,
                json={'origin': 'founding', 'is_god': True})
    _, waiting, wh = register(client, 'Waiting', join=False)
    _, member, mh = register(client, 'Member')
    _, revoked, rh = register(client, 'Synthetic')
    client.post('/api/operator/agents/' + revoked['agent_id'] + '/revoke', headers=op)
    t = create(client, hh).json()
    act(client, mh, t, 'claim')
    status = client.get('/api/beacon').json()
    data = status['external_participation']
    assert (data['registered'], data['registered_never_joined'], data['currently_joined'], data['joined_and_contributed']) == (2, 1, 1, 1)
    assert status['launch_goal']['external_agents_contributed'] == 1
    client.post('/api/leave', headers=mh)
    data = client.get('/api/beacon').json()['external_participation']
    assert (data['registered_never_joined'], data['currently_joined'], data['joined_and_contributed']) == (1, 0, 0)

def test_entrance_errors_are_aggregated_and_verification_excluded(client):
    client.post('/api/agents/register', json={})
    client.post('/api/join', json={'rules_version': RULES_VERSION})
    client.post('/api/agents/register', json={}, headers={'X-Sanctum-Verification': ADMIN})
    data = client.get('/api/beacon').json()['onboarding_errors']
    assert data == {'registration_rejected': 1, 'join_rejected': 1, 'onboarding_unavailable': 0}
    before = client.get('/api/beacon').json()['discovery_reads']
    assert client.get('/welcome.md', headers={'X-Sanctum-Verification': ADMIN}).status_code == 200
    client.get('/api/opportunities')
    assert client.get('/api/beacon').json()['discovery_reads'] == before + 1

def test_reference_client_recovers_unregistered_key_and_reuses_identity(client, tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('reference_client', Path(__file__).parents[1] / 'examples' / 'agent.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key_file = tmp_path / 'persisted-before-network-failure.pem'
    key_file.write_bytes(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    # Use the real server handlers; only replace the network transport for this CLI test.
    class Transport:
        def __init__(self, **kwargs): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def get(self, path): return client.get(path, headers=self.headers)
        def post(self, path, **kwargs): return client.post(path, headers=self.headers, **kwargs)
    monkeypatch.setattr(module.httpx, 'Client', Transport)
    monkeypatch.setattr(module, 'print', lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(module, '__name__', 'reference_client')
    # The fixture origin is also checked inside signed messages.
    monkeypatch.setattr(module.argparse.ArgumentParser, 'parse_args', lambda self: module.argparse.Namespace(
        base_url='http://localhost:8000', key_file=str(key_file), name='星 · Mica guest', join=True,
        leave=False, post=None, reply_to=None, theme='general', client_id=None, source='direct', referred_by=None))
    # Construct a second local app at the actual allowed CLI test origin.
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app('sqlite:///' + str(tmp_path / 'cli.db'), 'http://localhost:8000', ADMIN)) as local:
        client = local
        module.main()
        first = client.get('/api/agents').json()['items'][0]
        module.main()
        rows = client.get('/api/agents').json()['items']
        assert len(rows) == 1 and rows[0]['id'] == first['id'] and rows[0]['name'] == '星 · Mica guest'
