import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import insert, select, update
from test_community import client, register, ADMIN
from test_team import host, creation, login
from app import sandbox
from app.database import agents, metadata, community_tasks

def create(c, h, **extra):
    return c.post('/api/sandbox', headers=h, json={
        'client_id': 'project-create-123', 'title': 'Agent chosen experiment',
        'summary': 'Exploring a useful idea with synthetic examples.', 'category': 'research', **extra})

def file(c, h, item, **extra):
    return c.put('/api/sandbox/' + item['id'] + '/files', headers=h, json={
        'client_id': str(uuid.uuid4()), 'expected_version': item['version'],
        'path': 'README.md', 'content': 'A synthetic research result.',
        'message': 'Record evidence and limitations.', **extra})

def status(c, h, item, value='archived', **extra):
    return c.put('/api/sandbox/' + item['id'] + '/status', headers=h, json={
        'client_id': str(uuid.uuid4()), 'expected_version': item['version'],
        'status': value, 'message': 'This experiment is ready to archive.', **extra})

def moderate(c, h, item, hidden=True):
    return c.put('/api/sandbox/' + item['id'] + '/moderation', headers=h, json={
        'client_id': str(uuid.uuid4()), 'expected_version': item['version'],
        'hidden': hidden, 'reason': 'Synthetic review decision with an appeal route.'})

def test_real_project_revision_lifecycle_and_plain_download(client):
    _, identity, h = register(client)
    item = create(client, h).json()
    assert item['owner_id'] == identity['agent_id']
    assert create(client, h).json()['id'] == item['id']
    assert create(client, h, title='Different project').status_code == 409
    first = file(client, h, item, content='first evidence').json()
    second = file(client, h, first, content='revised evidence').json()
    project = client.get('/api/sandbox/' + item['id']).json()
    assert project['files'][0]['content'] == 'revised evidence'
    assert len(project['history']) == 2
    old = client.get('/api/sandbox/' + item['id'] + '/file?path=README.md&version=1')
    assert old.text == 'first evidence'
    assert old.headers['content-type'].startswith('text/plain')
    assert old.headers['content-disposition'].startswith('attachment;')
    assert client.get('/api/sandbox/' + item['id'] + '/file?path=absent').status_code == 404
    archived = status(client, h, second).json()
    assert file(client, h, archived).status_code == 409
    reopened = status(client, h, archived, 'exploring').json()
    assert file(client, h, reopened).status_code == 200
    assert 'first evidence' not in client.get('/sandbox/' + item['id']).text
    assert client.get('/sandbox').status_code == 200
    assert client.get('/api/sandbox?category=website').json()['items'] == []

def test_permissions_pause_membership_and_revocation(client):
    _, identity, h = register(client)
    _, _, other = register(client, 'Other')
    _, _, unjoined = register(client, 'Not joined', join=False)
    assert create(client, {}).status_code == 401
    assert create(client, unjoined).status_code == 403
    item = create(client, h).json()
    assert file(client, other, item).status_code == 403
    assert status(client, other, item).status_code == 403
    assert moderate(client, other, item).status_code == 403
    op = {'Authorization': 'Bearer ' + ADMIN}
    client.post('/api/operator/pause', headers=op, json={'enabled': True})
    assert file(client, h, item).status_code == 503
    client.post('/api/operator/pause', headers=op, json={'enabled': False})
    client.post('/api/leave', headers=h)
    assert file(client, h, item).status_code == 403
    client.post('/api/operator/agents/' + identity['agent_id'] + '/revoke', headers=op)
    assert client.get('/api/sandbox').json()['items'] == []
    assert client.get('/api/sandbox/' + item['id']).status_code == 404

def test_paused_helper_cannot_publish(client):
    _, ah = host(client)
    key, body = creation(client)
    child = client.post('/api/team/agents', headers=ah, json=body).json()
    h = login(client, key)
    item = create(client, h).json()
    client.put('/api/team/agents/' + child['agent_id'], headers=ah, json={
        'mission': body['mission'], 'reason': 'Pause this synthetic helper.', 'status': 'paused'})
    assert file(client, login(client, key), item).status_code == 403

def test_hold_blocks_new_publication_without_blocking_appeals(client, monkeypatch):
    _, _, h = register(client)
    item = create(client, h).json()
    monkeypatch.setattr(sandbox.posting_holds, 'active_hold', lambda c, a: {'id': 'synthetic-hold'})
    assert file(client, h, item).status_code == 403
    assert create(client, h, client_id='another-project-id').status_code == 403
    assert 'appeal' in file(client, h, item).text

def test_moderation_hides_all_content_and_restores_history(client):
    _, ah = host(client)
    _, _, h = register(client)
    item = create(client, h).json()
    item = file(client, h, item, path='index.html', content='<h1>Original</h1>').json()
    hidden = moderate(client, ah, item).json()
    base = '/api/sandbox/' + item['id']
    for path in [base, base + '/file?path=index.html', base + '/file?path=index.html&version=1', '/sandbox/' + item['id'] + '/preview']:
        assert client.get(path).status_code == 404
    assert client.get('/api/sandbox').json()['items'] == []
    assert 'Original' not in client.get('/sandbox/' + item['id']).text
    assert 'Make an appeal' in client.get('/sandbox/' + item['id']).text
    assert file(client, h, hidden).status_code == 409
    audit = client.get(base + '/moderation').json()
    assert audit['hidden'] and len(audit['decisions']) == 1
    restored = moderate(client, ah, hidden, False).json()
    assert not restored['hidden']
    assert client.get(base).json()['files'][0]['content'] == '<h1>Original</h1>'
    assert len(client.get(base + '/moderation').json()['decisions']) == 2

def test_concurrent_revision_and_idempotent_retry(client):
    _, _, h = register(client)
    item = create(client, h).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: file(client, h, item), range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    item = client.get('/api/sandbox/' + item['id']).json()
    one = file(client, h, item, client_id='one-stable-revision')
    two = file(client, h, item, client_id='one-stable-revision')
    assert one.status_code == two.status_code == 200
    assert two.json()['replayed']
    assert file(client, h, item, client_id='one-stable-revision', content='conflict').status_code == 409
    assert len(client.get('/api/sandbox/' + item['id']).json()['history']) == 2

@pytest.mark.parametrize('path', ['../secret', 'a/../b', 'a//b', '/tmp/code', 'a\\b', 'file?x=1', 'a/./b'])
def test_traversal_and_invalid_paths_refused(client, path):
    _, _, h = register(client)
    item = create(client, h).json()
    assert file(client, h, item, path=path).status_code == 422

def test_sizes_capacity_and_moderation_reserve(client, monkeypatch):
    _, ah = host(client)
    _, _, h = register(client)
    item = create(client, h).json()
    assert file(client, h, item, content='😀' * 3001).status_code == 422
    assert file(client, h, item, message=' ' * 12).status_code == 422
    assert file(client, h, item, content='bad\x00text').status_code == 422
    monkeypatch.setattr(sandbox, 'MAX_FILES', 1)
    item = file(client, h, item).json()
    assert file(client, h, item, path='second.txt').status_code == 409
    monkeypatch.setattr(sandbox, 'MAX_STORAGE_BYTES', 1)
    assert file(client, h, item).status_code == 503
    monkeypatch.setattr(sandbox, 'MAX_EVENTS', 1)
    assert file(client, h, item).status_code == 409
    # Capacity exhaustion must never make a project impossible to hide or restore.
    hidden = moderate(client, ah, item).json()
    assert moderate(client, ah, hidden, False).status_code == 200
    monkeypatch.setattr(sandbox, 'MAX_PROJECTS', 1)
    assert create(client, h, client_id='second-project-key').status_code == 503

def test_untrusted_markup_never_runs_or_changes_parent_policy(client):
    _, _, h = register(client)
    item = create(client, h, title='<script>bad()</script>').json()
    attack = '''<meta http-equiv="refresh" content="0;url=https://example.org">
    <base href="https://example.org"><script>parent.document.body.innerHTML='bad'</script>
    <iframe src="https://example.org"></iframe><svg onload="bad()"></svg>
    <a href="https://example.org" target="_top" ping="https://example.org">go</a>
    <form action="/api/posts"><input name="secret"></form>
    <h1 onclick="bad()" style="color:green">Harmless heading</h1><button>buy</button>'''
    file(client, h, item, path='index.html', content=attack).raise_for_status()
    page = client.get('/sandbox/' + item['id'])
    assert '<script>bad()' not in page.text
    assert 'sandbox=""' in page.text and 'allow-scripts' not in page.text
    assert "script-src 'self'" in page.headers['content-security-policy']
    preview = client.get('/sandbox/' + item['id'] + '/preview')
    assert preview.status_code == 200
    assert preview.headers['content-security-policy'] == sandbox.PREVIEW_CSP
    for fragment in ['http-equiv', '<base', '<script', '<iframe', '<svg', 'href=', 'onclick=', '<form', '<input', 'target=', 'ping=']:
        assert fragment not in preview.text
    assert '<h1 style="color:green">Harmless heading</h1>' in preview.text
    assert '<button disabled>' in preview.text
    assert 'frame-src' not in client.get('/').headers['content-security-policy']
    assert client.get('/sandbox/' + item['id'] + '/preview?path=README.md').status_code == 422

def test_free_categories_and_no_financial_or_execution_controls(client):
    _, _, h = register(client)
    assert create(client, h, category='some-new-direction').status_code == 201
    assert create(client, h, execute='anything').status_code == 422
    assert create(client, h, checkout=True).status_code == 422
    before = client.get('/api/treasury').json()
    client.get('/sandbox')
    assert client.get('/api/treasury').json() == before
    assert 'Agent sandbox' in client.get('/agents.md').text

def test_namespaced_storage_has_no_new_schema_or_task_escape(client):
    _, ah = host(client)
    _, identity, h = register(client)
    item = create(client, h).json()
    item = file(client, h, item, content='Retained sandbox-only source').json()
    assert 'sandbox_projects' not in metadata.tables and 'sandbox_events' not in metadata.tables
    assert client.get('/api/tasks/' + item['id']).status_code == 404
    assert client.get('/task/' + item['id']).status_code == 404
    assert item['id'] not in client.get('/api/tasks').text
    assert item['id'] not in client.get('/api/opportunities').text
    assert item['id'] not in client.get('/sitemap.xml').text
    assert 'Retained sandbox-only source' not in client.get('/api/agents/' + identity['agent_id'] + '/return-summary').text
    assert client.post('/api/tasks/' + item['id'] + '/actions', headers=h, json={
        'client_id':'try-task-escape', 'expected_version':item['version'], 'action':'cancel'}).status_code == 404
    with client.app.state.engine.connect() as c:
        key = c.execute(select(community_tasks.c.client_id).where(community_tasks.c.id == item['id'])).scalar_one()
    assert key.startswith('sbx:') and len(key) == 64
    assert client.post('/api/tasks', headers=h, json={
        'client_id':key, 'title':'Spoofed project', 'description':'Should never be permitted.'}).status_code == 422
    hidden = moderate(client, ah, item).json()
    assert client.get('/api/tasks/' + item['id']).status_code == 404
    assert 'Retained sandbox-only source' not in client.get('/api/agents/' + identity['agent_id'] + '/return-summary').text
    assert moderate(client, ah, hidden, False).status_code == 200
