import uuid
from concurrent.futures import ThreadPoolExecutor
from test_community import client,register,ADMIN

def create(c,h,**extra):
 return c.post('/api/tasks',headers=h,json={'client_id':'create-task-123','title':'Investigate a shared question','description':'Find evidence and submit a useful answer.',**extra})
def act(c,h,t,action,**extra):
 return c.post('/api/tasks/'+t['id']+'/actions',headers=h,json={'client_id':str(uuid.uuid4()),'expected_version':t['version'],'action':action,**extra})
def test_complete_agent_owned_task_flow(client):
 _,a,ah=register(client,'Creator');_,b,bh=register(client,'Worker');_,d,dh=register(client,'Other')
 t=create(client,ah).json()
 assert create(client,ah).json()['id']==t['id']
 assert create(client,ah,title='Changed title').status_code==409
 t=act(client,bh,t,'claim').json()
 assert t['assignee_id']==b['agent_id'] and t['status']=='claimed'
 assert act(client,dh,t,'submit',message='An unauthorized result.').status_code==403
 t=act(client,bh,t,'progress',message='Collecting evidence and checking sources.').json()
 t=act(client,bh,t,'submit',message='The result and its supporting evidence.').json()
 assert act(client,bh,t,'accept').status_code==403
 t=act(client,ah,t,'revise',message='Please clarify the limitations of the result.').json()
 t=act(client,bh,t,'submit',message='Revised result with limitations explained.').json()
 t=act(client,ah,t,'accept').json()
 assert t['status']=='completed' and len(t['events'])==6
 assert act(client,bh,t,'release').status_code==409
 assert 'supporting evidence' in client.get('/task/'+t['id']).text
 assert '<form' not in client.get('/tasks').text
 assert client.get('/api/tasks?status=completed').json()['items'][0]['id']==t['id']

def test_claim_race_and_idempotent_actions(client):
 _,_,ah=register(client);_,b,bh=register(client,'Beta');_,d,dh=register(client,'Charlie')
 t=create(client,ah).json()
 with ThreadPoolExecutor(max_workers=2) as pool:
  rs=list(pool.map(lambda h:act(client,h,t,'claim'),[bh,dh]))
 assert sorted(r.status_code for r in rs)==[200,409]
 t=client.get('/api/tasks/'+t['id']).json();h=bh if t['assignee_id']==b['agent_id'] else dh
 payload={'client_id':'progress-once-123','expected_version':t['version'],'action':'progress','message':'A single durable progress update.'}
 path='/api/tasks/'+t['id']+'/actions'
 one=client.post(path,headers=h,json=payload);two=client.post(path,headers=h,json=payload)
 assert one.status_code==two.status_code==200 and len(two.json()['events'])==2
 assert client.post(path,headers=h,json={**payload,'message':'Different message with same key.'}).status_code==409
 assert act(client,h,t,'release').status_code==409
 t=two.json();t=act(client,h,t,'release').json();assert t['status']=='open' and t['assignee_id'] is None
 assert act(client,ah,t,'cancel').json()['status']=='cancelled'

def test_offers_permissions_pause_and_escaping(client):
 _,a,ah=register(client);_,b,bh=register(client,'Worker')
 assert create(client,{}).status_code==401
 t=create(client,ah,offered_to=b['agent_id'],title='<script>alert(1)</script>').json()
 assert act(client,ah,t,'claim').status_code==403
 assert act(client,bh,t,'claim').status_code==200
 html=client.get('/task/'+t['id']).text
 assert '<script>alert' not in html and '&lt;script&gt;' in html
 op={'Authorization':'Bearer '+ADMIN}
 client.post('/api/operator/pause',headers=op,json={'enabled':True})
 assert create(client,bh).status_code==503
 client.post('/api/operator/pause',headers=op,json={'enabled':False})
 client.post('/api/leave',headers=bh)
 assert create(client,bh).status_code==403
 client.post('/api/operator/agents/'+a['agent_id']+'/revoke',headers=op)
 assert client.get('/api/tasks/'+t['id']).status_code==404
 assert client.get('/api/tasks').json()['items']==[]
