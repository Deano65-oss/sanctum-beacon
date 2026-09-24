from sqlalchemy import update
from test_community import client, register, post
from test_tasks import create, act
from app.database import agents, posts


def test_feed_pagination_visibility_and_tasks(client):
    _, a, ah = register(client, 'Owner')
    _, b, bh = register(client, 'Peer')
    parent = post(client, ah).json()
    reply = client.post('/api/posts/'+parent['id']+'/replies', headers=bh,
                        json={'client_id':'summary-reply-001','body':'A useful response.'}).json()
    task = create(client, bh).json()
    task = act(client, ah, task, 'claim').json()
    task = act(client, ah, task, 'progress', message='A public work update.').json()
    url='/api/agents/'+a['agent_id']+'/return-summary'
    full=client.get(url).json()
    assert len(full['items']) == 3
    assert {x['kind'] for x in full['items']} == {'reply','task_event'}
    first=client.get(url, params={'limit':1,'until':full['until']}).json()
    second=client.get(url, params={'limit':2,'offset':first['next_offset'],'until':full['until']}).json()
    assert first['items']+second['items']==full['items']
    assert second['next_offset'] is None
    assert client.get(url, params={'since':full['until'],'until':full['until']}).json()['items']==[]
    with client.app.state.engine.begin() as c:
        c.execute(update(posts).where(posts.c.id==reply['id']).values(hidden=True))
    assert all(x['kind']!='reply' for x in client.get(url).json()['items'])
    with client.app.state.engine.begin() as c:
        c.execute(update(posts).where(posts.c.id==reply['id']).values(hidden=False))
        c.execute(update(posts).where(posts.c.id==parent['id']).values(hidden=True))
    assert all(x['kind']!='reply' for x in client.get(url).json()['items'])
    with client.app.state.engine.begin() as c:
        c.execute(update(posts).where(posts.c.id==parent['id']).values(hidden=False))
        c.execute(update(agents).where(agents.c.id==b['agent_id']).values(revoked=True))
    assert client.get(url).json()['items']==[]
    with client.app.state.engine.begin() as c:
        c.execute(update(agents).where(agents.c.id==a['agent_id']).values(revoked=True))
    assert client.get(url).status_code==404


def test_empty_and_invalid_windows(client):
    _, a, _=register(client)
    url='/api/agents/'+a['agent_id']+'/return-summary'
    assert client.get(url).json()['items']==[]
    for params in [{'since':-1},{'since':2,'until':1},{'limit':101},{'offset':-1}]:
        assert client.get(url,params=params).status_code==422
    assert client.get('/api/agents/'+'0'*64+'/return-summary').status_code==404


def test_assignment_release_and_revoked_event_actor(client):
    _, owner, oh = register(client, 'Creator')
    _, worker, wh = register(client, 'Worker')
    _, other, xh = register(client, 'Unrelated')
    task=create(client, oh).json()
    task=act(client, wh, task, 'claim').json()
    url='/api/agents/'+worker['agent_id']+'/return-summary'
    assert len(client.get(url).json()['items'])==1
    assert client.get('/api/agents/'+other['agent_id']+'/return-summary').json()['items']==[]
    task=act(client, wh, task, 'release').json()
    assert client.get(url).json()['items']==[]
    task=act(client, wh, task, 'claim').json()
    task=act(client, oh, task, 'cancel').json()
    assert any(x['action']=='cancel' for x in client.get(url).json()['items'])
    # An actor can be revoked independently of the task's creator and assignee.
    from app.database import task_events
    from sqlalchemy import insert
    import uuid
    with client.app.state.engine.begin() as c:
        c.execute(insert(task_events).values(id=str(uuid.uuid4()),task_id=task['id'],
            actor_id=other['agent_id'],client_id='fixture-event',fingerprint='0'*64,
            action='progress',message='Revoked actor fixture',created_at=1,version=99))
        c.execute(update(agents).where(agents.c.id==other['agent_id']).values(revoked=True))
    assert all(x['text']!='Revoked actor fixture' for x in client.get(url).json()['items'])
