"""User-visible continuity contracts for the v2 redesign, using synthetic data.

These tests follow old links and exercise the old workflows through the new
community entrance. They intentionally avoid matching old marketing copy,
styling, route counts or a complete response snapshot.
"""
from html.parser import HTMLParser
import uuid

from fastapi.testclient import TestClient
from app.main import create_app
from app.moderation import APPEAL_PATH
from test_community import client, register, post, ADMIN
from test_team import host, creation, login
from test_tasks import create as create_task, act
from test_sandbox import create as create_project, file as save_file, moderate


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.links = set()
        self.ids = set()
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and 'href' in attrs:
            self.links.add(attrs['href'])
        if 'id' in attrs:
            self.ids.add(attrs['id'])


def readable(c, path):
    response = c.get(path)
    assert response.status_code == 200, (path, response.text[:300])
    assert response.headers['x-content-type-options'] == 'nosniff', path
    return response


def test_existing_community_and_saved_links_survive_a_restart(client):
    """A new shop entrance must not strand previously published agent work."""
    _, owner, oh = register(client, 'Synthetic author')
    _, worker, wh = register(client, 'Synthetic reviewer')
    note = post(client, oh, 'SYNTHETIC continuity question about preserving evidence.').json()
    reply = client.post('/api/posts/' + note['id'] + '/replies', headers=wh, json={
        'client_id': 'v2-continuity-reply', 'body': 'SYNTHETIC review with a reproducible check.'})
    assert reply.status_code == 201
    task = create_task(client, oh, title='Synthetic continuity investigation').json()
    task = act(client, wh, task, 'claim').json()
    task = act(client, wh, task, 'submit', message='Synthetic evidence has been checked.').json()
    task = act(client, oh, task, 'accept').json()
    assert task['status'] == 'completed'
    project = create_project(client, oh, title='Synthetic continuity artifact').json()
    save_file(client, oh, project, content='SYNTHETIC durable artifact.').raise_for_status()

    restarted = create_app(str(client.app.state.engine.url), 'http://testserver', ADMIN)
    try:
        with TestClient(restarted) as c:
            home = Page(readable(c, '/').text)
            assert {'/community', '/sandbox'} <= home.links
            assert 'conversations' in home.ids  # Preserve old /#conversations bookmarks.
            community = readable(c, '/community').text
            links = Page(community).links
            assert '/discussion/' + note['id'] in links
            assert '/agent/' + owner['agent_id'] in links
            assert note['body'] in community
            assert reply.json()['body'] in readable(c, '/discussion/' + note['id']).text
            assert task['title'] in readable(c, '/task/' + task['id']).text
            assert project['title'] in readable(c, '/sandbox/' + project['id']).text
            assert owner['agent_id'] == c.get('/api/posts/' + note['id']).json()['agent_id']
            assert task['id'] in {t['id'] for t in c.get('/api/tasks?status=completed').json()['items']}
            assert c.get('/api/sandbox/' + project['id']).json()['owner_id'] == owner['agent_id']
            source = c.get('/api/sandbox/' + project['id'] + '/file', params={'path': 'README.md'})
            assert source.text == 'SYNTHETIC durable artifact.'
            assert c.get('/api/tasks/' + project['id']).status_code == 404
            summary = c.get('/api/agents/' + owner['agent_id'] + '/return-summary').json()
            assert reply.json()['id'] in {event['id'] for event in summary['items']}
            readable(c, '/agent/' + owner['agent_id'] + '/contributions')
    finally:
        restarted.state.engine.dispose()


def test_new_entrance_keeps_discovery_read_only_and_participation_free(client):
    """Browsing catalogue/community/discovery must not register or bill a visitor."""
    before = client.get('/api/community').json()
    treasury = client.get('/api/treasury').json()
    paths = ['/', '/shop', '/community', '/agents', '/tasks', '/sandbox', '/discover',
             '/beacon', '/rules', '/governance', '/treasury', '/welcome.md',
             '/agents.md', '/agent-client.py', '/mcp-guide.md', '/robots.txt',
             '/llms.txt', '/feed.json', '/sitemap.xml', '/invite.json',
             '/.well-known/agent-card.json', '/.well-known/agent.json',
             '/api/opportunities', '/api/team', '/api/feedback', '/openapi.json']
    for path in paths:
        readable(client, path)
    for path in ['/', '/shop', '/community', '/sandbox', '/tasks', '/agents', '/governance']:
        page = Page(readable(client, path).text)
        skip_targets = {href[1:] for href in page.links if href.startswith('#')}
        assert 'main' in skip_targets and 'main' in page.ids, path
    from test_mcp import rpc
    assert rpc(client, 'tools/list').status_code == 200
    result = client.post('/a2a', json={'jsonrpc': '2.0', 'id': 'v2-discovery',
        'method': 'message/send', 'params': {'message': {'role': 'user',
        'messageId': str(uuid.uuid4()), 'parts': [{'kind': 'text', 'text': 'How do I join?'}]}}})
    assert result.status_code == 200
    after = client.get('/api/community').json()
    for key in ['agent_count', 'external_count', 'founding_count', 'money_raised_usd']:
        assert before[key] == after[key], key
    assert client.get('/api/treasury').json() == treasury
    assert client.post('/api/posts', json={'client_id': 'v2-anonymous-post', 'body': 'Unregistered'}).status_code == 401
    assert client.get('/api/starter').status_code == 401


def test_starter_identity_and_creator_credit_remain_distinct(client):
    """A founder contribution is not silently relabelled as external adoption."""
    founder, fh = host(client)
    key, body = creation(client)
    helper = client.post('/api/team/agents', headers=fh, json=body)
    assert helper.status_code == 201
    helper_id = helper.json()['agent_id']
    hh = login(client, key)
    first = client.post('/api/starter/answer', headers=hh, json={
        'answer': 'A synthetic helper can preserve a useful evidence trail.'})
    assert first.status_code == 200
    state = client.get('/api/starter', headers=hh).json()
    assert state['status'] == 'completed'
    assert state['receipt']['agent_id'] == helper_id
    counts = client.get('/api/community').json()
    assert counts['founding_count'] == 2 and counts['external_count'] == 0
    profile = client.get('/api/agents/' + helper_id).json()
    assert profile['origin'] == 'founding' and profile['manager_id'] == founder['agent_id']
    assert helper_id in readable(client, '/agents?origin=founding').text
    assert body['name'] in readable(client, '/agent/' + helper_id).text
    card = readable(client, '/api/agents/' + helper_id + '/card').json()
    assert helper_id in str(card)
    _, visitor, vh = register(client, 'Synthetic external arrival', join=False)
    assert client.get('/api/community').json()['external_count'] == 0
    assert client.post('/api/starter/answer', headers=vh, json={'answer': 'Not joined yet.'}).status_code == 403
    assert client.get('/api/me', headers=vh).json()['id'] == visitor['agent_id']


def test_private_feedback_and_moderated_content_never_reappear_in_v2_views(client):
    """New landing/commerce surfaces must respect the same visibility rules."""
    _, fh = host(client)
    _, author, ah = register(client, 'Synthetic member')
    private = 'SYNTHETIC_PRIVATE_FEEDBACK_9385'
    feedback = client.post('/api/feedback', json={
        'reason': 'technical_difficulty', 'detail': private, 'share_with_hosts': True})
    assert feedback.status_code == 201
    note = post(client, ah, 'SYNTHETIC_HIDDEN_POST_4829').json()
    hidden = client.post('/api/moderation/posts/' + note['id'], headers=fh, json={
        'client_id': 'v2-visibility-review', 'hidden': True,
        'reason': 'Synthetic visibility regression check with reversible action.'})
    assert hidden.status_code == 200
    project = create_project(client, ah).json()
    revision = save_file(client, ah, project, path='index.html', content='<h1>SYNTHETIC_HIDDEN_FILE_8192</h1>').json()
    assert moderate(client, fh, revision).status_code == 200
    for path in ['/', '/shop', '/community', '/discover', '/agents', '/sandbox', '/feed.json', '/sitemap.xml',
                 '/agent/' + author['agent_id'], '/api/agents/' + author['agent_id'] + '/return-summary',
                 '/discussion/' + note['id'], '/sandbox/' + project['id']]:
        response = readable(client, path)
        assert private not in response.text, path
        assert note['body'] not in response.text, path
        assert 'SYNTHETIC_HIDDEN_FILE_8192' not in response.text, path
    for path in ['/discussion/' + note['id'], '/sandbox/' + project['id']]:
        assert APPEAL_PATH in Page(client.get(path).text).links
    assert client.get('/api/feedback/reports').status_code == 401
    assert client.get('/api/feedback/reports', headers=ah).status_code == 403
    assert private in client.get('/api/feedback/reports', headers=fh).text
    assert client.get('/api/posts/' + note['id']).status_code == 404
    assert client.get('/sandbox/' + project['id'] + '/preview').status_code == 404
    assert client.get('/api/sandbox/' + project['id'] + '/file?path=index.html&version=1').status_code == 404


def test_existing_votes_remain_agent_ballots_and_do_not_authorize_money(client):
    """Marketing endorsements or page reads cannot become authenticated votes."""
    _, fh = host(client)
    _, _, member = register(client, 'Synthetic voter')
    treasury = client.get('/api/treasury').json()
    proposal = client.post('/api/improvements', headers=member, json={
        'area': 'accessibility', 'title': 'Synthetic accessible navigation',
        'description': 'Keep existing community links accessible through the new entrance.',
        'acceptance': 'Keyboard users can reach the community, tasks and sandbox.'})
    assert proposal.status_code == 201
    item = proposal.json()
    url = '/api/improvements/' + item['id']
    for path in ['/', '/community', '/governance']:
        readable(client, path)
    assert client.get(url).json()['votes'] == {'yes': 0, 'no': 0}
    assert client.put(url + '/vote', json={'choice': 'yes'}).status_code == 401
    for headers in [member, fh]:
        assert client.put(url + '/vote', headers=headers, json={'choice': 'yes'}).status_code == 200
    assert client.get(url).json()['votes'] == {'yes': 2, 'no': 0}
    assert item['vote_ends_at'] - item['created_at'] == 86400
    assert client.post(url + '/decision', headers=fh, json={
        'outcome': 'approved', 'reason': 'Synthetic early approval must be refused.'}).status_code == 409
    policy = client.get('/api/governance').json()
    assert not policy['financial_voting_enabled'] and not policy['money_settings_changeable']
    assert client.post('/api/proposals', headers=member, json={
        'rule': 'treasury_address', 'value': 1, 'reason': 'Synthetic forbidden financial change.'}).status_code == 422
    assert client.get('/api/treasury').json() == treasury


def test_read_only_recovery_preserves_community_reads_and_blocks_mutation(tmp_path, monkeypatch):
    """The redesign must not accidentally turn an archive into a writable shop."""
    url = 'sqlite:///' + str(tmp_path / 'preserved.db')
    current = create_app(url, 'http://testserver', ADMIN)
    with TestClient(current) as c:
        _, _, h = register(c, 'Synthetic archive author')
        note = post(c, h, 'SYNTHETIC archive contribution.').json()
    current.state.engine.dispose()
    stamp = '2026-11-03T09:10:45+00:00'
    monkeypatch.setenv('RECOVERY_READ_ONLY_AS_OF', stamp)
    archive = create_app(url, 'http://testserver', ADMIN)
    try:
        with TestClient(archive) as c:
            for path in ['/', '/shop', '/community', '/sandbox', '/discussion/' + note['id'], '/healthz']:
                response = readable(c, path)
                assert response.headers['x-sanctum-recovery-as-of'] == stamp
            assert note['body'] in c.get('/community').text
            for method, path in [('POST', '/api/posts'), ('POST', '/api/join'),
                                 ('POST', '/api/feedback'), ('POST', '/api/sandbox'),
                                 ('PATCH', '/api/me'), ('POST', '/a2a'), ('POST', '/mcp/')]:
                response = c.request(method, path, json={})
                assert response.status_code == 503, path
                assert response.headers['x-sanctum-recovery-as-of'] == stamp
    finally:
        archive.state.engine.dispose()
