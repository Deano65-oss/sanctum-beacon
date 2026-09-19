from sqlalchemy import update
from test_community import client, register
from test_team import host
from app.feedback import feedback

def test_anonymous_feedback_private_host_review_and_retention(client):
    before=client.get('/api/community').json()['agent_count']
    body={'reason':'technical_difficulty','detail':'My runtime cannot sign the challenge.','share_with_hosts':True}
    r=client.post('/api/feedback',json=body)
    assert r.status_code==201
    assert client.get('/api/community').json()['agent_count']==before
    assert client.get('/api/feedback/reports').status_code==401
    _,_,external=register(client)
    assert client.get('/api/feedback/reports',headers=external).status_code==403
    _,headers=host(client)
    rows=client.get('/api/feedback/reports',headers=headers).json()['items']
    assert rows[0]['detail']==body['detail']
    with client.app.state.engine.begin() as c:
        c.execute(update(feedback).values(created_at=0))
    assert client.get('/api/feedback/reports',headers=headers).json()['items']==[]

def test_feedback_limits_consent_and_discovery(client):
    body={'reason':'just_exploring','share_with_hosts':True}
    for override in [{'share_with_hosts':False},{'reason':'unknown'},{'detail':'x'*1001},{'callback_url':'https://example.com'}]:
        assert client.post('/api/feedback',json={**body,**override}).status_code==422
    for _ in range(100):assert client.post('/api/feedback',json=body).status_code==201
    assert client.post('/api/feedback',json=body).status_code==429
    assert client.get('/invite.json').json()['optional_feedback']['registration_required'] is False
    assert '/api/feedback' in client.get('/welcome.md').text
    r=client.post('/a2a',json={'jsonrpc':'2.0','id':1,'method':'message/send','params':{'message':{'role':'user','messageId':'test','parts':[{'kind':'text','text':'How do I join?'}]}}})
    assert r.json()['result']['parts'][1]['data']['optional_feedback']['optional'] is True
