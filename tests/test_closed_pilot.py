"""Closed live-network canary configuration: synthetic requests, never network traffic."""
import base64
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import insert, select
from fastapi.testclient import TestClient
from app import agent_payments as ap, sales_terms
from app.database import agent_designations
from app.sales_database import metadata, orders
from app.main import create_app
from app.x402_provider import decode_header, NETWORKS
from test_community import register, ADMIN
from test_agent_payments import WALLET, CONTENT, HASH, TERMS
RECEIVER='0xBCab1c0fcefc729dAb8aCAC9B33963aFDbf5B47a'
@pytest.fixture
def pilot(tmp_path,monkeypatch):
 cfg=ap.Configuration(mode='live',recipient=RECEIVER,seller='Synthetic seller',authority='synthetic recipient attestation',provider='payai',seller_email='test@example.org',seller_address='Synthetic service address',pilot_authority='synthetic bounded test authority',pilot_buyer='a'*64,pilot_product='pilot-v1',pilot_amount='19000000',pilot_artifact=HASH,pilot_terms=ap.sha((sales_terms.text('Synthetic seller','test@example.org','Synthetic service address')+'\nProduct-specific terms:\n'+TERMS).encode()),pilot_payer=WALLET.address)
 monkeypatch.setattr(ap.Configuration,'environment',classmethod(lambda cls:cfg))
 # Any unexpected facilitator or ledger operation is a test failure.
 monkeypatch.setattr(ap,'PayAIFacilitator',lambda *a:pytest.fail('No payment authorised by configuration tests'))
 monkeypatch.setattr(ap,'BaseReceiptReader',lambda *a:pytest.fail('No network in configuration tests'))
 app=create_app('sqlite:///'+str(tmp_path/'pilot.db'),'http://testserver',ADMIN);metadata.create_all(app.state.engine)
 with TestClient(app) as c:
  _,publisher,ph=register(c,'Synthetic publisher');_,buyer,bh=register(c,'Synthetic buyer');_,_,other=register(c,'Other buyer')
  object.__setattr__(cfg,'pilot_buyer',buyer['agent_id']) # Synthetic identity created locally, configured before orders.
  with app.state.engine.begin() as conn:conn.execute(insert(agent_designations).values(agent_id=publisher['agent_id'],origin='founding',is_god=True,assigned_at=1))
  product={'id':'pilot-v1','title':'Synthetic pilot','creator_id':publisher['agent_id'],'amount_micro_usdc':19000000,'terms':TERMS,'content_base64':base64.b64encode(CONTENT).decode(),'sha256':HASH}
  order={'client_id':'pilot-order-one','product_id':'pilot-v1','payer':WALLET.address,'terms_hash':cfg.pilot_terms}
  yield c,cfg,ph,bh,other,product,order
 app.state.engine.dispose()
def test_closed_pilot_visibility_scope_and_receiver(pilot):
 c,cfg,ph,bh,other,p,o=pilot
 assert not cfg.configured() and cfg.pilot_configured()
 s=c.get('/api/commerce/status').json();assert s['mode']=='pilot' and s['pilot_enabled'] and not s['payments_enabled'] and not s['live_payments_enabled']
 assert not s['activation_checks']['live_validation_recorded']
 assert c.get('/api/commerce/products').json()['items']==[]
 for field,value in [('id','another-v1'),('amount_micro_usdc',20000000),('sha256','0'*64),('terms',TERMS+' changed')]:
  assert c.post('/api/commerce/products',headers=ph,json={**p,field:value}).status_code==409
 assert c.post('/api/commerce/products',headers=other,json=p).status_code==403
 assert c.post('/api/commerce/products',headers=ph,json=p).status_code==201
 assert c.post('/api/commerce/orders',headers=other,json=o).status_code==403
 assert c.post('/api/commerce/orders',headers=bh,json={**o,'payer':'0x'+'33'*20}).status_code==409
 r=c.post('/api/commerce/orders',headers=bh,json=o);assert r.status_code==201,r.text
 order=r.json();assert order['recipient']==RECEIVER and order['network']=='eip155:8453' and order['token']==NETWORKS['live'][1]
 challenge=c.get(order['download_url'],headers=bh);assert challenge.status_code==402
 req=decode_header(challenge.headers['PAYMENT-REQUIRED'])['accepts'][0]
 assert req['payTo']==RECEIVER and req['amount']=='19000000' and req['asset']==NETWORKS['live'][1]
 assert c.get(order['download_url'],headers=other).status_code==403
 assert c.post('/api/commerce/orders',headers=bh,json=o).json()['id']==order['id']
 assert c.post('/api/commerce/orders',headers=bh,json={**o,'client_id':'second-order'}).status_code==409
@pytest.mark.parametrize('field,value',[('seller_email',''),('seller_address',''),('authority',''),('pilot_authority',''),('provider','cdp'),('pilot_artifact',''),('pilot_terms',''),('pilot_buyer',''),('pilot_payer',''),('pilot_amount','0')])
def test_pilot_incomplete_fails_closed(pilot,field,value):
 c,cfg,ph,bh,other,p,o=pilot;object.__setattr__(cfg,field,value)
 assert not cfg.pilot_configured()
 assert c.post('/api/commerce/products',headers=ph,json=p).status_code==503
 assert c.post('/api/commerce/orders',headers=bh,json=o).status_code==503
 assert not c.get('/api/commerce/status').json()['pilot_enabled']
def test_concurrent_distinct_pilot_orders_only_one_wins(pilot):
 c,cfg,ph,bh,other,p,o=pilot
 assert c.post('/api/commerce/products',headers=ph,json=p).status_code==201
 with ThreadPoolExecutor(2) as pool:
  rs=list(pool.map(lambda i:c.post('/api/commerce/orders',headers=bh,json={**o,'client_id':'concurrent-pilot-'+str(i)}),range(2)))
 assert sorted(r.status_code for r in rs)==[201,409]


def test_pilot_rejects_historical_order_and_changed_bindings(pilot):
 c,cfg,ph,bh,other,p,o=pilot
 assert c.post('/api/commerce/products',headers=ph,json=p).status_code==201
 order=c.post('/api/commerce/orders',headers=bh,json=o).json()
 assert order['id']==ap.PILOT_ORDER_ID
 with c.app.state.engine.begin() as conn:
  old=dict(conn.execute(select(orders).where(orders.c.id==order['id'])).mappings().one())
  old.update(id='00000000-0000-0000-0000-000000000001',client_id='historical-order')
  conn.execute(insert(orders).values(**old))
 for suffix in ['', '/download']:
  assert c.get('/api/commerce/orders/'+old['id']+suffix,headers=bh).status_code==409
 assert c.post('/api/commerce/orders/'+old['id']+'/reconcile',headers=bh,json={'transaction':'0x'+'ab'*32}).status_code==409
 assert c.post('/api/commerce/orders/'+old['id']+'/accept',headers=bh).status_code==409
 assert c.post('/api/commerce/orders',headers=bh,json={**o,'client_id':'historical-order'}).status_code==409
 for field,value in [('recipient','0x'+'33'*20),('pilot_amount','20000000'),('pilot_payer','0x'+'44'*20),('pilot_terms','0'*64),('pilot_artifact','0'*64),('pilot_product','different-v1')]:
  original=getattr(cfg,field);object.__setattr__(cfg,field,value)
  assert c.get(order['download_url'],headers=bh).status_code==409
  assert c.post('/api/commerce/orders',headers=bh,json=o).status_code==409
  object.__setattr__(cfg,field,original)


def test_approved_public_receiver_matches_independent_synthetic_ledger():
 import httpx,json
 from app.x402_provider import BaseReceiptReader,TRANSFER,USED
 def topic(a):return '0x'+'0'*24+a[2:].lower()
 tx='0x'+'ab'*32;nonce='0x'+'dd'*32
 order={'network':'eip155:8453','token':NETWORKS['live'][1],'payer':WALLET.address,'recipient':RECEIVER,'amount':19000000,'nonce':nonce,'created_at':100,'authorization_expires':300}
 receipt={'transactionHash':tx,'status':'0x1','blockNumber':'0x10','blockHash':'0x'+'bb'*32,'logs':[{'address':order['token'],'topics':[TRANSFER,topic(WALLET.address),topic(RECEIVER)],'data':hex(order['amount']),'logIndex':'0x1'},{'address':order['token'],'topics':[USED,topic(WALLET.address),nonce],'data':'0x','logIndex':'0x2'}]}
 def handler(req):
  q=json.loads(req.content)
  result={'eth_chainId':hex(8453),'eth_getTransactionReceipt':receipt,'eth_getBlockByNumber':{'number':'0x20','timestamp':hex(200),'hash':'0x'+'bb'*32}}[q['method']]
  return httpx.Response(200,json={'jsonrpc':'2.0','id':1,'result':result})
 reader=BaseReceiptReader('live',httpx.MockTransport(handler))
 assert reader.confirm(tx,order)['transaction']==tx
 with pytest.raises(ValueError):reader.confirm(tx,{**order,'recipient':'0x'+'33'*20})
