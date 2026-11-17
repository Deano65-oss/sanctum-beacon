"""Local synthetic x402 interoperability and failure tests. No real transfers."""
import base64, hashlib, json, os
from concurrent.futures import ThreadPoolExecutor
import httpx, pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update
from x402.mechanisms.evm.exact.client import ExactEvmScheme
from x402.schemas import PaymentRequirements, PaymentRequired
from app.main import create_app
from app import agent_payments as ap
from app.sales_database import metadata, products, orders, budgets
from app.x402_provider import CdpFacilitator, BaseReceiptReader, encode_header, decode_header, TRANSFER, USED
from test_community import register, ADMIN
# Public test-only fixture. Never funded or used against a network.
WALLET=Account.from_key(bytes.fromhex('11'*32)); RECIPIENT='0x'+'22'*20; TX='0x'+'aa'*32
CONTENT=b'PK\x03\x04synthetic-only'; HASH=hashlib.sha256(CONTENT).hexdigest()
TERMS='Synthetic test terms. No live product, account or purchase.'
class FakeProvider:
 def __init__(self): self.calls=[];self.ambiguous=False
 def call(self,op,payload,required):
  self.calls.append(op)
  if op=='verify': return {'isValid':True,'payer':WALLET.address}
  if self.ambiguous: raise TimeoutError('synthetic timeout')
  return {'success':True,'payer':WALLET.address,'network':required['network'],'transaction':TX}
class FakeReader:
 def __init__(self):self.final=True
 def confirm(self,tx,order):
  return {'transaction':tx,'log_index':1,'block_hash':'0x'+'bb'*32,'block_number':10} if self.final else None
@pytest.fixture
def setup(tmp_path,monkeypatch):
 for k,v in {'SANCTUM_X402_MODE':'testnet','SANCTUM_X402_RECIPIENT':RECIPIENT,'SANCTUM_X402_SELLER':'Synthetic seller','SANCTUM_X402_AUTHORITY':'test','SANCTUM_CDP_API_KEY_ID':'test','SANCTUM_CDP_API_KEY_SECRET':'test'}.items():monkeypatch.setenv(k,v)
 provider,reader=FakeProvider(),FakeReader()
 monkeypatch.setattr(ap,'CdpFacilitator',lambda *a:provider);monkeypatch.setattr(ap,'BaseReceiptReader',lambda *a:reader)
 url=os.getenv('SANCTUM_X402_TEST_PG')
 if url: assert '/sanctum_x402_test?' in url and 'host=/private/tmp/sanctum-x402-' in url
 app=create_app(url or 'sqlite:///'+str(tmp_path/'test.db'),'http://testserver',ADMIN);metadata.create_all(app.state.engine)
 with app.state.engine.begin() as c:c.execute(insert(products).values(id='synthetic-v1',title='Synthetic product',creator_id='a'*64,sha256=HASH,amount=19000000,terms=TERMS,content=CONTENT,status='listed'))
 with TestClient(app) as c:
  _,_,h=register(c);yield c,h,provider,reader
 if url:
  from app.database import metadata as community_metadata
  metadata.drop_all(app.state.engine);community_metadata.drop_all(app.state.engine)
 app.state.engine.dispose()
def create(s,cid='synthetic-order-1'):
 c,h,_,_=s;r=c.post('/api/commerce/orders',headers=h,json={'client_id':cid,'product_id':'synthetic-v1','payer':WALLET.address,'terms_hash':ap.sha(TERMS.encode())})
 assert r.status_code==201,r.text
 return r.json()
def payment(s,o):
 c,h,_,_=s;r=c.get(o['download_url'],headers=h);assert r.status_code==402,r.text
 challenge=decode_header(r.headers['PAYMENT-REQUIRED']);PaymentRequired.model_validate(challenge)
 required=challenge['accepts'][0]
 payload={'x402Version':2,'accepted':required,'payload':ExactEvmScheme(WALLET).create_payment_payload(PaymentRequirements.model_validate(required)),'resource':challenge['resource']}
 sig='0x'+Account.sign_message(encode_defunct(text=o['wallet_proof_message']),WALLET.key).signature.hex()
 return payload,{**h,'PAYMENT-SIGNATURE':encode_header(payload),'X-Sanctum-Wallet-Proof':sig}
def test_disabled_no_schema_or_network(tmp_path,monkeypatch):
 monkeypatch.delenv('SANCTUM_X402_MODE',raising=False)
 app=create_app('sqlite:///'+str(tmp_path/'off.db'),'http://testserver',ADMIN)
 with TestClient(app) as c:
  assert not c.get('/api/commerce/status').json()['payments_enabled']
  assert c.get('/api/commerce/products').json()['items']==[]
  assert 'Checkout is not open yet' in c.get('/shop/payments').text
  assert 'prove control without a payment' in c.get('/shop/payments/receiver').text
  _,_,h=register(c);assert c.get('/api/commerce/orders/missing/download',headers=h).status_code==503
 assert not ap.inspect(app.state.engine).has_table('sales_orders');app.state.engine.dispose()
def test_live_gate():
 assert not ap.Configuration('live',RECIPIENT,'seller','authority','id','secret').configured()
 assert not ap.Configuration('testnet','0x'+'0'*40,'seller','authority','id','secret').configured()
def test_sdk_payment_delivery_acceptance(setup):
 c,h,p,_=setup;o=create(setup);_,ph=payment(setup,o)
 r=c.get(o['download_url'],headers=ph);assert r.status_code==202,r.text;assert r.json()['status']=='paid'
 assert p.calls==['verify','settle']
 for _ in range(2):assert c.get(o['download_url'],headers=h).content==CONTENT
 assert p.calls==['verify','settle']
 assert c.post('/api/commerce/orders/'+o['id']+'/accept',headers=h).status_code==200
 assert c.get('/api/commerce/orders/'+o['id'],headers=h).json()['accepted_at']
 assert not c.get('/api/commerce/orders/'+o['id'],headers=h).json()['earned_revenue']
def test_private_and_idempotent(setup):
 c,h,p,_=setup;o=create(setup);assert create(setup)['id']==o['id'];_,_,other=register(c,'Another synthetic agent')
 for path in ['/api/commerce/orders/'+o['id'],o['download_url']]:assert c.get(path,headers=other).status_code==404
 assert c.post('/api/commerce/orders/'+o['id']+'/accept',headers=h).status_code==409
 assert not p.calls
@pytest.mark.parametrize('field,value',[('amount','1'),('network','eip155:1'),('payTo','0x'+'33'*20),('asset','0x'+'44'*20),('scheme','upto')])
def test_wrong_requirements(setup,field,value):
 c,_,p,_=setup;o=create(setup);payload,h=payment(setup,o);payload['accepted'][field]=value;h['PAYMENT-SIGNATURE']=encode_header(payload)
 assert c.get(o['download_url'],headers=h).status_code==422;assert not p.calls
def test_forged_payment_and_wallet_proof(setup):
 c,_,p,_=setup;o=create(setup);payload,h=payment(setup,o);payload['payload']['authorization']['value']='1';h['PAYMENT-SIGNATURE']=encode_header(payload)
 assert c.get(o['download_url'],headers=h).status_code==422
 _,h=payment(setup,o);h['X-Sanctum-Wallet-Proof']='0x'+'00'*65
 assert c.get(o['download_url'],headers=h).status_code==422;assert not p.calls
def test_timeout_reconcile_without_repayment(setup):
 c,h,p,_=setup;p.ambiguous=True;o=create(setup);_,ph=payment(setup,o)
 assert c.get(o['download_url'],headers=ph).json()['status']=='review'
 assert c.get(o['download_url'],headers=ph).status_code==202;assert p.calls==['verify','settle']
 assert c.post('/api/commerce/orders/'+o['id']+'/reconcile',headers=h,json={'transaction':TX}).json()['finalized_payment_verified']
 assert c.get(o['download_url'],headers=h).content==CONTENT
def test_finality_holds_download(setup):
 c,h,_,reader=setup;reader.final=False;o=create(setup);_,ph=payment(setup,o)
 assert c.get(o['download_url'],headers=ph).json()['status']=='settling'
 assert c.get(o['download_url'],headers=h).status_code==202
 reader.final=True;assert c.post('/api/commerce/orders/'+o['id']+'/reconcile',headers=h,json={'transaction':TX}).status_code==200
 assert c.get(o['download_url'],headers=h).content==CONTENT
def test_nonce_replay(setup):
 c,_,p,r=setup;r.final=False;a=create(setup);payload,h=payment(setup,a);assert c.get(a['download_url'],headers=h).status_code==202
 b=create(setup,'synthetic-order-2');_,bh=payment(setup,b);bh['PAYMENT-SIGNATURE']=encode_header(payload)
 assert c.get(b['download_url'],headers=bh).status_code==409;assert p.calls.count('settle')==1
def test_concurrent_settlement(setup):
 c,_,p,r=setup;r.final=False;o=create(setup);_,h=payment(setup,o)
 with ThreadPoolExecutor(2) as pool:responses=list(pool.map(lambda _:c.get(o['download_url'],headers=h),range(2)))
 assert all(r.status_code in (202,409) for r in responses);assert p.calls.count('settle')==1
def test_budget_stops_before_settlement(setup):
 c,_,p,_=setup
 with c.app.state.engine.begin() as conn:conn.execute(insert(budgets).values(month=ap.datetime.now(ap.timezone.utc).strftime('%Y-%m'),attempts=100))
 o=create(setup);_,h=payment(setup,o);assert c.get(o['download_url'],headers=h).status_code==503;assert p.calls==['verify']
 with c.app.state.engine.connect() as conn:assert conn.execute(select(orders.c.status)).scalar_one()=='awaiting'
def test_expired_and_tampered_artifact(setup):
 c,_,p,_=setup;o=create(setup);_,h=payment(setup,o)
 with c.app.state.engine.begin() as conn:conn.execute(update(orders).values(expires_at=1))
 assert c.get(o['download_url'],headers=h).status_code==409
 with c.app.state.engine.begin() as conn:conn.execute(update(products).values(content=b'changed'))
 assert c.get(o['download_url'],headers=h).status_code==503;assert not p.calls
def test_auth_origin_and_no_redirects():
 from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
 import jwt
 key=Ed25519PrivateKey.generate();secret=base64.b64encode(key.private_bytes_raw()+key.public_key().public_bytes_raw()).decode();seen=[]
 def handler(req):
  seen.append(req);claims=jwt.decode(req.headers['Authorization'].removeprefix('Bearer '),key.public_key(),algorithms=['EdDSA'],options={'verify_aud':False})
  assert claims['uris']==['GET api.cdp.coinbase.com/platform/v2/x402/supported']
  return httpx.Response(302,headers={'Location':'https://example.org/steal'})
 with pytest.raises(httpx.HTTPStatusError):CdpFacilitator('synthetic-api-id',secret,httpx.MockTransport(handler)).call('supported')
 assert len(seen)==1 and seen[0].url.host=='api.cdp.coinbase.com'
def test_finalized_rpc_transfer_and_nonce():
 nonce='0x'+'dd'*32
 def topic(a):return '0x'+'0'*24+a[2:].lower()
 o={'network':'eip155:84532','token':ap.NETWORKS['testnet'][1],'payer':WALLET.address,'recipient':RECIPIENT,'amount':19000000,'nonce':nonce,'created_at':100,'authorization_expires':300}
 r={'transactionHash':TX,'status':'0x1','blockNumber':'0x10','blockHash':'0x'+'bb'*32,'logs':[
 {'address':o['token'],'topics':[TRANSFER,topic(WALLET.address),topic(RECIPIENT)],'data':hex(19000000),'logIndex':'0x1'},
 {'address':o['token'],'topics':[USED,topic(WALLET.address),nonce],'data':'0x','logIndex':'0x2'}]}
 responses={'eth_chainId':hex(84532),'eth_getTransactionReceipt':r,'eth_getBlockByNumber':{'number':'0x20','hash':r['blockHash'],'timestamp':hex(200)}}
 def handler(req):return httpx.Response(200,json={'jsonrpc':'2.0','id':1,'result':responses[json.loads(req.content)['method']]})
 reader=BaseReceiptReader('testnet',httpx.MockTransport(handler));assert reader.confirm(TX,o)['log_index']==1
 r['logs'][0]['data']='0x1'
 with pytest.raises(ValueError):reader.confirm(TX,o)
 r['logs'][0]['data']=hex(19000000);r['logs'][1]['topics'][2]='0x'+'ee'*32
 with pytest.raises(ValueError):reader.confirm(TX,o)
def test_bad_header():
 with pytest.raises(ValueError):decode_header('!')
 with pytest.raises(ValueError):decode_header(base64.b64encode(b'{"a":1,"a":2}').decode())


def test_disabled_receiver_configuration_visible_without_secrets(tmp_path,monkeypatch):
 monkeypatch.setenv('SANCTUM_X402_MODE','disabled')
 monkeypatch.setenv('SANCTUM_X402_RECIPIENT',RECIPIENT)
 monkeypatch.setenv('SANCTUM_X402_AUTHORITY','synthetic-attestation')
 monkeypatch.setenv('SANCTUM_CDP_API_KEY_SECRET','must-not-be-returned')
 app=create_app('sqlite:///'+str(tmp_path/'configured.db'),'http://testserver',ADMIN)
 with TestClient(app) as c:
  r=c.get('/api/commerce/status');d=r.json()
  assert d['configured_recipient']==RECIPIENT and not d['payments_enabled']
  assert d['activation_checks']['recipient_authority_recorded']
  assert 'must-not-be-returned' not in r.text
  page=c.get('/shop/payments').text
  assert 'Checkout is not open yet' in page
  assert 'Agent checkout unavailable' in page and 'disabled' in page
  assert 'Browser wallet checkout isn’t available yet.' in page
  assert RECIPIENT in page
 app.state.engine.dispose()


def test_agent_purchase_handoff_binds_exact_edition(setup):
 c,h,provider,_=setup
 page=c.get('/shop/payments').text
 assert 'Test purchases only' in page
 assert 'synthetic-v1' in page and HASH in page and ap.sha(TERMS.encode()) in page
 assert 'X-Sanctum-Wallet-Proof' in page and 'PAYMENT-SIGNATURE' in page
 assert not provider.calls
 # The rendered identity and terms create an order for this exact edition;
 # altering terms must fail before any payment provider interaction.
 assert c.post('/api/commerce/orders',headers=h,json={'client_id':'handoff-wrong-terms','product_id':'synthetic-v1','payer':WALLET.address,'terms_hash':'0'*64}).status_code==409
 order=create(setup,'handoff-valid-order')
 assert order['sha256']==HASH and order['recipient']==RECIPIENT
 assert not provider.calls
