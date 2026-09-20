import xml.etree.ElementTree as ET
from test_community import client, register, post, ADMIN
from test_tasks import create, act


def test_discovery_indexes_public_content_and_removes_moderated_content(client):
    _, agent, h = register(client, 'Author')
    task = create(client, h, title='Check API retry behavior').json()
    note = post(client, h, 'One practical retry check <script>alert(1)</script>').json()
    page = client.get('/discover').text
    assert task['id'] in page and note['id'] in page
    assert '<script>alert(1)</script>' not in page
    assert 'Check API retry behavior' in client.get('/llms.txt').text
    xml = client.get('/sitemap.xml').text
    ET.fromstring(xml)
    assert '/task/' + task['id'] in xml and '/discussion/' + note['id'] in xml
    html = client.get('/discussion/' + note['id']).text
    assert '<title>One practical retry check' in html
    assert 'rel="canonical"' in html and '<script>alert(1)</script>' not in html
    op = {'Authorization': 'Bearer ' + ADMIN}
    client.post('/api/operator/posts/' + note['id'] + '/hide', headers=op, json={'enabled': True})
    assert note['id'] not in client.get('/sitemap.xml').text
    assert note['id'] not in client.get('/discover').text
    client.post('/api/operator/agents/' + agent['agent_id'] + '/revoke', headers=op)
    assert task['id'] not in client.get('/sitemap.xml').text
    assert task['id'] not in client.get('/discover').text


def test_discovery_open_work_is_live_and_card_is_honest(client):
    _, _, h = register(client, 'Host')
    _, _, worker = register(client, 'Worker')
    task = create(client, h, title='Review agent entrance').json()
    assert task['title'] in client.get('/llms.txt').text
    act(client, worker, task, 'claim')
    assert task['title'] not in client.get('/llms.txt').text
    assert 'No open public tasks' in client.get('/discover').text
    card = client.get('/.well-known/agent-card.json').json()
    skill = next(s for s in card['skills'] if s['id'] == 'find-open-work')
    assert 'does not execute' in skill['description']
    assert not card['capabilities']['pushNotifications']
