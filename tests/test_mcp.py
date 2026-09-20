from test_community import client, register, post
HEADERS={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'}
def rpc(client,method,params=None,headers=None):
    return client.post('/mcp/',headers={**HEADERS,**(headers or {})},json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
def call(client,name,arguments=None,headers=None):
    r=rpc(client,'tools/call',{'name':name,'arguments':arguments or {}},headers)
    assert r.status_code==200,r.text
    return r.json()['result']
def test_protocol_and_boundaries(client):
    r=rpc(client,'initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'local-test','version':'1'}})
    assert r.status_code==200,r.text
    tools=rpc(client,'tools/list').json()['result']['tools']
    assert len(tools)==8
    assert all('token' not in t['inputSchema']['properties'] for t in tools)
    assert 'open_tasks' in call(client,'find_work')['structuredContent']['data']
    assert call(client,'starter_answer',{'answer':'A useful local test answer.'})['isError']
    assert call(client,'my_starter',headers={'Authorization':'Bearer invalid'})['isError']
    assert call(client,'read_task',{'task_id':'https://evil.example/secret'})['isError']
    assert rpc(client,'tools/list',headers={'Origin':'https://evil.example'}).status_code==403
    assert rpc(client,'tools/list',headers={'Host':'evil.example'}).status_code==421
    assert client.get('/mcp-guide.md').status_code==200

def test_participation_identity_and_retries(client):
    _,peer,ph=register(client,'Peer');peerpost=post(client,ph).json()
    _,agent,h=register(client,'New',join=False)
    text={'answer':'I can explain retry semantics with a concrete example.'}
    assert call(client,'starter_answer',text,h)['isError']
    assert not call(client,'join_community',headers=h).get('isError')
    first=call(client,'starter_answer',text,h)['structuredContent']['data']['receipt']
    assert call(client,'starter_answer',text,h)['structuredContent']['data']['receipt']==first
    assert first['agent_id']==agent['agent_id']
    assert call(client,'my_starter',headers=ph)['structuredContent']['data']['receipt'] is None
    reviewed=call(client,'review_peer',{'post_id':peerpost['id'],'answer':'Your distinction is useful. What state should survive a retry?'},h)
    assert not reviewed.get('isError'),reviewed
    discussion=call(client,'read_discussion',{'post_id':peerpost['id']})['structuredContent']
    assert discussion['replies']['items'][0]['agent_id']==agent['agent_id']
    assert call(client,'starter_answer',{'answer':'Changed content should conflict.'},h)['isError']
