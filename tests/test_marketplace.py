"""Marketplace permission, evidence, concurrency and no-payment boundaries."""
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from test_community import client, register, ADMIN
from test_team import host, creation, login
from app import marketplace
from app.database import community_tasks, metadata


def fields(**extra):
    return dict(title='Synthetic comparison kit', summary='A public synthetic calculator fixture for testing migrations.',
        category='tools', kind='free_artifact', price='0.00', currency='GBP', provenance='creator',
        provenance_url='https://example.org/project', rights_statement='I created these synthetic examples and can share them.',
        rights_confirmed=True, delivery_notes='The public source reference contains the downloadable text artifact.',
        artifact_url='https://example.org/project/README.txt', public_material_only=True, **extra)


def listing(c, h, **changes):
    body=fields();body.update(client_id=str(uuid.uuid4()),status='listed');body.update(changes)
    return c.post('/api/marketplace/listings',headers=h,json=body)


def edit(c,h,item,**changes):
    body=fields();body.update(client_id=str(uuid.uuid4()),expected_version=item['version'],message='Revise the documented public scope.');body.update(changes)
    return c.put('/api/marketplace/listings/'+item['id'],headers=h,json=body)


def state(c,h,item,value='archived',**changes):
    body=dict(client_id=str(uuid.uuid4()),expected_version=item['version'],status=value,message='Archive or reopen this public listing.');body.update(changes)
    return c.put('/api/marketplace/listings/'+item['id']+'/status',headers=h,json=body)


def moderate(c,h,item,hidden=True):
    return c.put('/api/marketplace/listings/'+item['id']+'/moderation',headers=h,json={
        'client_id':str(uuid.uuid4()),'expected_version':item['version'],'hidden':hidden,
        'reason':'Synthetic moderation review; use the existing appeal route.'})


def request(c,h,item,**changes):
    body=dict(client_id=str(uuid.uuid4()),listing_id=item['id'],listing_version=item['version'],
        request='Please discuss this synthetic comparison scope before any commitment.',
        acknowledge_non_binding=True,public_material_only=True);body.update(changes)
    return c.post('/api/marketplace/orders',headers=h,json=body)


def event(c,h,item,action='message',**changes):
    body=dict(client_id=str(uuid.uuid4()),expected_version=item['version'],action=action,
        message='A public synthetic clarification with no payment commitment.',
        acknowledge_non_binding=True,public_material_only=True);body.update(changes)
    return c.post('/api/marketplace/orders/'+item['id']+'/events',headers=h,json=body)


def test_listing_draft_publish_edit_archive_and_stable_replay(client):
    _,identity,h=register(client)
    item=listing(client,h,status='draft',client_id='stable-create-listing').json()
    assert item['status']=='draft' and item['owner_id']==identity['agent_id']
    assert client.get('/api/marketplace/listings').json()['items']==[]
    assert client.get('/api/marketplace/listings/'+item['id']).status_code==404
    assert client.get('/api/marketplace/listings/mine',headers=h).json()['items'][0]['id']==item['id']
    published=state(client,h,item,'listed').json()
    assert client.get('/api/marketplace/listings/'+item['id']).json()['title']=='Synthetic comparison kit'
    revised=edit(client,h,published,title='Revised synthetic kit',client_id='stable-revision-key')
    assert revised.status_code==200
    assert edit(client,h,published,title='Revised synthetic kit',client_id='stable-revision-key').json()['replayed']
    assert edit(client,h,published,title='Other title',client_id='stable-revision-key').status_code==409
    assert listing(client,h,status='draft',client_id='stable-create-listing').json()['id']==item['id']
    assert listing(client,h,status='draft',client_id='stable-create-listing',title='Different creation').status_code==409
    history=client.get('/api/marketplace/listings/'+item['id']+'/history',headers=h).json()['history']
    assert [e['version'] for e in history]==[0,1,2]
    assert history[0]['change']['initial']['title']=='Synthetic comparison kit'
    assert client.get('/api/marketplace/listings/'+item['id']+'/history').status_code==401
    archived=state(client,h,revised.json()).json()
    assert client.get('/api/marketplace/listings').json()['items']==[]
    assert edit(client,h,archived).status_code==409
    assert state(client,h,archived).status_code==409
    assert state(client,h,archived,'listed').status_code==200


def test_live_permissions_pause_revocation_and_helpers(client):
    _,owner,h=register(client)
    _,_,other=register(client,'Other')
    _,_,unjoined=register(client,'Unjoined',join=False)
    assert listing(client,{}).status_code==401
    assert listing(client,unjoined).status_code==403
    item=listing(client,h).json()
    assert edit(client,other,item).status_code==403
    assert state(client,other,item).status_code==403
    assert moderate(client,other,item).status_code==403
    assert client.get('/api/marketplace/listings/'+item['id']+'/history',headers=other).status_code==404
    op={'Authorization':'Bearer '+ADMIN}
    client.post('/api/operator/pause',headers=op,json={'enabled':True}).raise_for_status()
    assert edit(client,h,item).status_code==503
    client.post('/api/operator/pause',headers=op,json={'enabled':False}).raise_for_status()
    client.post('/api/leave',headers=h).raise_for_status()
    assert edit(client,h,item).status_code==403
    assert request(client,other,item).status_code==403
    client.post('/api/operator/agents/'+owner['agent_id']+'/revoke',headers=op).raise_for_status()
    assert client.get('/api/marketplace/listings').json()['items']==[]
    assert client.get('/api/marketplace/listings/'+item['id']).status_code==404


def test_paused_helper_and_hold_cannot_publish_but_archive_reduces_exposure(client,monkeypatch):
    _,ah=host(client)
    key,body=creation(client)
    helper=client.post('/api/team/agents',headers=ah,json=body).json()
    h=login(client,key);item=listing(client,h).json()
    monkeypatch.setattr(marketplace.posting_holds,'active_hold',lambda c,a:{'id':'synthetic-hold'})
    assert edit(client,h,item).status_code==403
    assert listing(client,h).status_code==403
    archived=state(client,h,item).json()
    assert archived['status']=='archived'
    assert state(client,h,archived,'listed').status_code==403
    client.put('/api/team/agents/'+helper['agent_id'],headers=ah,json={
        'mission':body['mission'],'reason':'Pause this synthetic helper for the check.','status':'paused'}).raise_for_status()
    assert listing(client,login(client,key)).status_code==403


def test_nonbinding_request_permissions_history_and_participant_consent(client):
    _,ah=host(client)
    _,seller,sh=register(client,'Seller')
    _,buyer,bh=register(client,'Buyer')
    _,_,oh=register(client,'Stranger')
    item=listing(client,sh).json()
    assert request(client,sh,item).status_code==409
    order=request(client,bh,item,client_id='stable-request-key').json()
    assert order['non_binding'] and order['financial_status']=='not_transactable'
    assert order['buyer_id']==buyer['agent_id'] and order['seller_id']==seller['agent_id']
    assert request(client,bh,item,client_id='stable-request-key').json()['id']==order['id']
    assert request(client,bh,item,client_id='stable-request-key',request='A different synthetic request scope.').status_code==409
    base='/api/marketplace/orders/'+order['id']
    assert client.get(base).status_code==401
    assert client.get(base,headers=oh).status_code==404
    assert client.get('/api/marketplace/orders',headers=oh).json()['items']==[]
    for h in [bh,sh,ah]:
        r=client.get(base,headers=h);assert r.status_code==200 and r.headers['cache-control']=='no-store'
    assert event(client,ah,order,'acknowledge').status_code==403
    assert event(client,bh,order,'acknowledge').status_code==403
    assert event(client,sh,order,'withdraw').status_code==403
    clarified=event(client,sh,order,'acknowledge').json()
    assert clarified['status']=='clarifying' and clarified['non_binding']
    updated=event(client,bh,clarified,client_id='stable-order-event').json()
    assert event(client,bh,clarified,client_id='stable-order-event').json()['replayed']
    withdrawn=event(client,bh,updated,'withdraw').json()
    assert withdrawn['status']=='withdrawn'
    assert event(client,sh,withdrawn).status_code==409
    assert len(client.get(base,headers=sh).json()['history'])==4


def test_price_snapshot_and_version_prevent_silent_term_changes(client):
    _,_,sh=register(client,'Seller');_,_,bh=register(client,'Buyer')
    item=listing(client,sh,kind='custom_commission',price='75.00',artifact_url=None).json()
    order=request(client,bh,item).json()
    revised=edit(client,sh,item,kind='custom_commission',price='90.00',artifact_url=None).json()
    assert request(client,bh,item).status_code==409
    assert request(client,bh,revised).json()['listing']['price']=='90.00'
    original=client.get('/api/marketplace/orders/'+order['id'],headers=bh).json()
    assert original['listing']['price']=='75.00' and original['listing']['version']==0
    assert 'accepted' not in original['status']


def test_hidden_content_is_not_available_through_public_or_task_routes(client):
    _,ah=host(client);_,owner,sh=register(client,'Seller');_,_,bh=register(client,'Buyer')
    item=listing(client,sh,title='Unique hidden market title').json()
    order=request(client,bh,item).json()
    hidden=moderate(client,ah,item).json()
    assert hidden['hidden']
    assert client.get('/api/marketplace/listings').json()['items']==[]
    assert client.get('/api/marketplace/listings/'+item['id']).status_code==404
    assert request(client,bh,hidden).status_code==404
    assert edit(client,sh,hidden).status_code==409
    scoped=client.get('/api/marketplace/orders/'+order['id'],headers=bh).json()
    assert scoped['listing']=={'id':item['id'],'unavailable':True}
    assert 'Unique hidden market title' not in str(scoped)
    assert event(client,sh,order).status_code==409
    assert event(client,bh,order,'withdraw').status_code==200
    for identifier in [item['id'],order['id']]:
        assert client.get('/api/tasks/'+identifier).status_code==404
        assert client.get('/task/'+identifier).status_code==404
        assert identifier not in client.get('/api/tasks').text
        assert identifier not in client.get('/api/opportunities').text
        assert identifier not in client.get('/sitemap.xml').text
        assert client.post('/api/tasks/'+identifier+'/actions',headers=sh,json={
            'client_id':'namespace-mutation-probe','expected_version':0,'action':'cancel'}).status_code==404
        assert client.get('/api/sandbox/'+identifier).status_code==404
    assert 'Unique hidden market title' not in client.get('/api/agents/'+owner['agent_id']+'/return-summary').text
    audit=client.get('/api/marketplace/listings/'+item['id']+'/moderation').json()
    assert audit['hidden'] and len(audit['decisions'])==1 and 'appeal_path' in audit
    restored=moderate(client,ah,hidden,False).json()
    assert not restored['hidden'] and client.get('/api/marketplace/listings/'+item['id']).status_code==200


@pytest.mark.parametrize('url',[
    'http://example.org/a','javascript:alert(1)','https://localhost/a','https://127.0.0.1/a',
    'https://[::1]/a','https://user:password@example.org/a','https://example.org:8080/a',
    'https://example.org/\nsecret','https://example.org\\@private.local/a',
    'https://checkout.stripe.com/pay/test','https://www.paypal.com/pay/test'])
def test_unsafe_and_payment_references_rejected(client,url):
    _,_,h=register(client)
    assert listing(client,h,provenance_url=url).status_code==422
    assert listing(client,h,artifact_url=url).status_code==422


@pytest.mark.parametrize('changes',[
    {'price':'-1.00'},{'price':'1e3'},{'price':'1.2'},{'price':'00.00'},{'price':'1.00'},
    {'kind':'custom_commission','price':'0.00','artifact_url':None},
    {'kind':'custom_commission','price':'50.00'},
    {'rights_confirmed':False},{'public_material_only':False},{'artifact_url':None},
    {'checkout_url':'https://example.org/pay'},{'payment_received':True},{'kind':'nft'}])
def test_unpriced_unsupported_and_financial_listing_fields_rejected(client,changes):
    _,_,h=register(client)
    assert listing(client,h,**changes).status_code==422


def test_no_payment_acceptance_or_settlement_state_and_no_treasury_changes(client):
    _,_,sh=register(client,'Seller');_,_,bh=register(client,'Buyer')
    before=client.get('/api/treasury').json()
    item=listing(client,sh,provenance='sourced').json()
    assert item['provenance']=='sourced' and item['provenance_verified'] is False
    assert request(client,bh,item,acknowledge_non_binding=False).status_code==422
    assert request(client,bh,item,payment_token='fictional').status_code==422
    order=request(client,bh,item).json()
    for action in ['accept','pay','settle','fulfill','purchase','refund']:
        assert event(client,sh,order,action).status_code==422
    p=client.get('/api/marketplace').json()
    assert not p['checkout_enabled'] and not p['payments_enabled'] and not p['order_requests_binding']
    assert client.get('/api/treasury').json()==before


def test_concurrent_writes_have_one_winner(client):
    _,_,sh=register(client,'Seller');_,_,bh=register(client,'Buyer')
    item=listing(client,sh).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:edit(client,sh,item),range(2)))
    assert sorted(x.status_code for x in outcomes)==[200,409]
    current=client.get('/api/marketplace/listings/'+item['id']).json()
    order=request(client,bh,current).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:event(client,bh,order),range(2)))
    assert sorted(x.status_code for x in outcomes)==[200,409]


def test_capacity_keeps_moderation_archive_and_closing_available(client,monkeypatch):
    _,ah=host(client);_,_,sh=register(client,'Seller');_,_,bh=register(client,'Buyer')
    item=listing(client,sh).json();order=request(client,bh,item).json()
    monkeypatch.setattr(marketplace,'MAX_STORAGE_BYTES',1)
    assert edit(client,sh,item).status_code==503
    assert request(client,bh,item).status_code==503
    assert event(client,bh,order).status_code==503
    monkeypatch.setattr(marketplace,'MAX_REVISIONS',0)
    hidden=moderate(client,ah,item).json()
    restored=moderate(client,ah,hidden,False).json()
    assert state(client,sh,restored).status_code==200
    assert event(client,bh,order,'withdraw').status_code==200
    monkeypatch.setattr(marketplace,'MAX_LISTINGS',1)
    assert listing(client,sh).status_code==503


def test_existing_schema_reserved_namespace_and_daily_caps(client):
    _,_,h=register(client)
    item=listing(client,h).json()
    with client.app.state.engine.connect() as c:
        key=c.execute(select(community_tasks.c.client_id).where(community_tasks.c.id==item['id'])).scalar_one()
    assert key.startswith('shop:l:') and len(key)==64
    assert not any(name.startswith('marketplace_') for name in metadata.tables)
    assert client.post('/api/tasks',headers=h,json={'client_id':key,'title':'Reserved spoof','description':'Must not be allowed.'}).status_code==422
    assert listing(client,h).status_code==201
    assert listing(client,h).status_code==201
    blocked=listing(client,h)
    assert blocked.status_code==429 and int(blocked.headers['retry-after'])>0


@pytest.mark.parametrize('field',['title','summary','delivery_notes'])
def test_postgresql_unsupported_text_rejected_before_storage(client,field):
    _,_,h=register(client)
    assert listing(client,h,**{field:'Synthetic text with a NUL \x00 character'}).status_code==422


def test_counterparty_departure_or_hold_stops_contact_without_blocking_closure(client,monkeypatch):
    _,_,sh=register(client,'Seller');_,buyer,bh=register(client,'Buyer')
    item=listing(client,sh).json();order=request(client,bh,item).json()
    monkeypatch.setattr(marketplace.posting_holds,'active_hold',lambda c,a:{'id':'synthetic-hold'} if a==buyer['agent_id'] else None)
    assert event(client,sh,order).status_code==403
    assert client.get('/api/marketplace/orders/'+order['id'],headers=bh).status_code==200
    monkeypatch.setattr(marketplace.posting_holds,'active_hold',lambda c,a:None)
    client.post('/api/leave',headers=bh).raise_for_status()
    assert event(client,sh,order).status_code==403
    assert client.get('/api/marketplace/orders',headers=bh).status_code==403
    assert client.get('/api/marketplace/orders/'+order['id'],headers=bh).status_code==403
    assert event(client,sh,order,'decline').status_code==200


def test_terminal_transition_and_creation_retries_remain_idempotent(client):
    _,_,sh=register(client,'Seller');_,_,bh=register(client,'Buyer')
    item=listing(client,sh).json();order=request(client,bh,item,client_id='idempotent-request').json()
    first=event(client,bh,order,'withdraw',client_id='idempotent-withdrawal')
    repeated=event(client,bh,order,'withdraw',client_id='idempotent-withdrawal')
    assert first.status_code==repeated.status_code==200
    assert repeated.json()['replayed'] and repeated.json()['version']==first.json()['version']
    assert event(client,bh,order,'withdraw',client_id='idempotent-withdrawal',message='Changed withdrawal text should conflict.').status_code==409
    assert event(client,bh,first.json(),'withdraw').status_code==409
    retried_request=request(client,bh,item,client_id='idempotent-request')
    assert retried_request.status_code==201 and retried_request.json()['status']=='withdrawn'
    assert len(retried_request.json()['history'])==2
