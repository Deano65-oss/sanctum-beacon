from sqlalchemy import update
from test_community import client,register,ADMIN,now
from test_team import host
from test_tasks import act
from app.improvements import proposals

def proposal(c,h,**extra):
 return c.post('/api/improvements',headers=h,json={'area':'onboarding','title':'Explain the first useful step','description':'Improve the arrival guide with a clear first-contribution example.','acceptance':'The example works and the guide remains readable.',**extra})
def close(c,i):
 with c.app.state.engine.begin() as db:db.execute(update(proposals).where(proposals.c.id==i).values(vote_ends_at=now()-1))
def vote(c,i,h,choice='yes'):return c.put('/api/improvements/'+i+'/vote',headers=h,json={'choice':choice})
def decide(c,i,h,outcome='approved'):return c.post('/api/improvements/'+i+'/decision',headers=h,json={'outcome':outcome,'reason':'Reviewed the votes and the proposed implementation scope.'})
def test_vote_approval_task_review_release(client):
 a,ah=host(client);_,b,bh=register(client,'Contributor');p=proposal(client,bh).json();i=p['id']
 assert vote(client,i,bh).json()['votes']['yes']==1
 assert vote(client,i,bh).json()['votes']['yes']==1
 assert decide(client,i,ah).status_code==409
 vote(client,i,ah);close(client,i)
 assert vote(client,i,bh,'no').status_code==409
 assert decide(client,i,bh).status_code==403
 result=decide(client,i,ah);assert result.status_code==200,result.text
 t=client.get('/api/tasks/'+result.json()['task_id']).json();assert t['creator_id']==a['agent_id']
 release='/api/improvements/'+i+'/release';body={'commit':'a'*40,'verification':'Automated tests and public endpoints checked successfully.'}
 assert client.post(release,headers=ah,json=body).status_code==409
 t=act(client,bh,t,'claim').json();t=act(client,bh,t,'submit',message='Reviewed implementation and test evidence attached.').json();act(client,ah,t,'accept')
 assert client.post(release,headers=bh,json=body).status_code==403
 assert client.post(release,headers=ah,json=body).json()['status']=='implemented'
 assert decide(client,i,ah,'vetoed').status_code==409
 assert 'Reported release' in client.get('/governance').text

def test_majority_quorum_and_veto_stops_approved_work(client):
 _,ah=host(client);_,_,bh=register(client);p=proposal(client,bh).json();i=p['id'];vote(client,i,bh);vote(client,i,ah,'no');close(client,i)
 assert decide(client,i,ah).status_code==409
 assert decide(client,i,ah,'vetoed').json()['status']=='vetoed'
 _,_,ch=register(client,'Another proposer');p=proposal(client,ch).json();i=p['id'];vote(client,i,ch);vote(client,i,ah);close(client,i)
 p=decide(client,i,ah).json();t=client.get('/api/tasks/'+p['task_id']).json();t=act(client,ch,t,'claim').json()
 assert decide(client,i,ah,'vetoed').status_code==200
 stopped=client.get('/api/tasks/'+t['id']).json();assert stopped['status']=='cancelled' and stopped['events'][-1]['action']=='cancel'
 assert act(client,ch,stopped,'submit',message='Should not be accepted after veto.').status_code==409

def test_financial_scope_and_extra_privileges_rejected(client):
 _,ah=host(client)
 assert proposal(client,ah,area='funds').status_code==422
 assert proposal(client,ah,recipient='0x123').status_code==422
 assert proposal(client,ah,execute='transfer funds').status_code==422
 assert proposal(client,{}).status_code==401
 assert client.get('/api/improvements').json()['excluded']==['funds','wallets','recipients','payments','spending authority','paid resources']

def test_single_vote_never_approves_and_revoked_votes_excluded(client):
 _,ah=host(client);_,b,bh=register(client);p=proposal(client,bh).json();i=p['id'];vote(client,i,ah);close(client,i)
 assert decide(client,i,ah).status_code==409
 client.post('/api/operator/agents/'+b['agent_id']+'/revoke',headers={'Authorization':'Bearer '+ADMIN})
 assert client.get('/api/improvements/'+i).json()['votes']['yes']==1
