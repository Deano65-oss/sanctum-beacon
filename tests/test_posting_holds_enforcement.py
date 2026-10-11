import uuid
from sqlalchemy import insert,select,func
from test_community import client,register,post
from test_mcp import call
from test_posting_holds_routes import setup,decision
from app import posting_holds as h
from app.database import posts,limits
from app.moderation import APPEAL_PATH


def held(c,duration=86400):
    a,ah,b,bh,ids=setup(c)
    _,peer,ph=register(c,'Peer')
    parent=post(c,ph,body='Relevant discussion for review.').json()
    original=post(c,bh,body='Previously accepted contribution.').json()
    appeal=APPEAL_PATH.rsplit('/',1)[-1]
    with c.app.state.engine.begin() as db:
        db.execute(insert(posts).values(id=appeal,agent_id=a['agent_id'],
            client_id='synthetic-appeal-thread',body='Synthetic appeal discussion.',
            theme='shared-questions',parent_id=None,hidden=False,created_at=original['created_at']))
    payload=decision(ids);payload['duration']=duration
    response=c.post('/api/moderation/accounts/'+b['agent_id']+'/posting-holds',headers=ah,json=payload)
    response.raise_for_status()
    return a,ah,b,bh,parent,original,appeal,response.json()


def test_http_and_mcp_block_new_posts_preserve_retry_and_allow_appeal(client):
    a,ah,b,bh,parent,original,appeal,hold=held(client)
    with client.app.state.engine.connect() as db:
        count=db.execute(select(func.count()).select_from(posts)).scalar()
        quota=list(db.execute(select(limits)).mappings())
    rejection=post(client,bh,body='New held contribution.')
    assert rejection.status_code==403 and rejection.json()['detail']['hold']==hold
    assert client.post('/api/posts/'+parent['id']+'/replies',headers=bh,
        json={'client_id':str(uuid.uuid4()),'body':'Another held reply.'}).status_code==403
    assert call(client,'starter_answer',{'answer':'An otherwise useful starter contribution.'},bh)['isError']
    assert call(client,'review_peer',{'post_id':parent['id'],'answer':'A relevant review that is currently held.'},bh)['isError']
    with client.app.state.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(posts)).scalar()==count
        assert list(db.execute(select(limits)).mappings())==quota
    retry={k:original[k] for k in ['client_id','body','theme']}
    r=client.post('/api/posts',headers=bh,json=retry);r.raise_for_status()
    assert r.json()['id']==original['id']
    r=client.post('/api/posts/'+appeal+'/replies',headers=bh,
        json={'client_id':str(uuid.uuid4()),'body':'Please review my specific evidence and relevance.'})
    r.raise_for_status()
    assert not call(client,'review_peer',{'post_id':appeal,'answer':'Please review this hold in the designated appeal thread.'},bh).get('isError')
    assert client.get('/api/posts/'+parent['id']).status_code==200
    assert client.get('/api/tasks').status_code==200
    assert post(client,ah,body='Ordinary unheld founder contribution.').status_code==201


def test_expiry_audit_survives_failed_publication_then_allows_work(client,monkeypatch):
    a,ah,b,bh,parent,original,appeal,hold=held(client,1)
    monkeypatch.setattr(h.time,'time',lambda:hold['expires_at'])
    # The invalid parent rejects publication after expiry evaluation. Expiry's
    # outer transaction must persist without a content write or quota charge.
    r=client.post('/api/posts/missing-parent/replies',headers=bh,
        json={'client_id':str(uuid.uuid4()),'body':'This cannot publish.'})
    assert r.status_code==404
    events=client.get('/api/moderation/posting-hold-events').json()['items']
    assert [e['action'] for e in events]==['create','expire']
    assert post(client,bh,body='Publication after hold expiry.').status_code==201
    assert [e['action'] for e in client.get('/api/moderation/posting-hold-events').json()['items']]==['create','expire']


def test_successful_reply_retry_survives_hidden_parent_during_hold(client):
    from test_moderation import route
    a,ah,b,bh,parent,original,appeal,hold=held(client)
    # Release briefly to capture an accepted reply, then create a fresh hold
    # from new evidence. No prior hold evidence is reused.
    client.post('/api/moderation/posting-holds/'+hold['id']+'/release',headers=ah,
        json={'client_id':'retry-fixture-release','reason':'Release for the accepted reply fixture.'}).raise_for_status()
    path='/api/posts/'+parent['id']+'/replies'
    payload={'client_id':'reply-original-retry','body':'Accepted reply before the next hold.'}
    accepted=client.post(path,headers=bh,json=payload);accepted.raise_for_status()
    ids=[]
    for text in ['Fresh first spam evidence.','Fresh second spam evidence.']:
        item=post(client,bh,body=text).json();ids.append(item['id'])
        route(client,ah,item['id'],key='hide-'+item['id']).raise_for_status()
    body=decision(ids);body['client_id']='retry-fixture-new-hold'
    client.post('/api/moderation/accounts/'+b['agent_id']+'/posting-holds',headers=ah,json=body).raise_for_status()
    route(client,ah,parent['id'],key='hide-parent-retry-fixture').raise_for_status()
    with client.app.state.engine.connect() as db:
        count=db.execute(select(func.count()).select_from(posts)).scalar()
        counters=list(db.execute(select(limits)).mappings())
    retry=client.post(path,headers=bh,json=payload);retry.raise_for_status()
    assert retry.json()==accepted.json()
    assert client.post(path,headers=bh,json={**payload,'body':'Changed retry content.'}).status_code==409
    with client.app.state.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(posts)).scalar()==count
        assert list(db.execute(select(limits)).mappings())==counters
