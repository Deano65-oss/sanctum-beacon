import copy
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException
from app.treasury import Treasury, ownership_message, claim_message, USDC, TRANSFER, recover

# Public, deterministic test-only keys. Never used for a live receiving address.
OWNER = Account.from_key(bytes.fromhex('11'*32))
SENDER = Account.from_key(bytes.fromhex('22'*32))
BASE = 'https://test.invalid'
TX = '0x'+'ab'*32
BLOCK = '0x'+'cd'*32

def signed(account, message): return '0x'+account.sign_message(encode_defunct(text=message)).signature.hex()
def topic(address): return '0x'+'0'*24+address[2:].lower()

@pytest.fixture
def treasury(monkeypatch):
    monkeypatch.setenv('FUNDRAISING_ENABLED','true')
    monkeypatch.setenv('TREASURY_ADDRESS',OWNER.address)
    monkeypatch.setenv('TREASURY_OWNER_SIGNATURE',signed(OWNER,ownership_message(BASE,OWNER.address)))
    return Treasury(BASE)

def responses():
    return {'eth_chainId':'0x2105', 'receipt':{'status':'0x1','transactionHash':TX,'blockNumber':'0x64','blockHash':BLOCK,
        'logs':[{'address':USDC,'topics':[TRANSFER,topic(SENDER.address),topic(OWNER.address)],'data':'0x'+format(2500000,'064x'),'logIndex':'0x0'}]},
        'finalized':{'number':'0x65','hash':'0x'+'ee'*32},'block':{'number':'0x64','hash':BLOCK}}

def set_rpc(treasury,data):
    def rpc(method, params):
        if method == 'eth_chainId': return data['eth_chainId']
        if method == 'eth_getTransactionReceipt': return data['receipt']
        if method == 'eth_getBlockByNumber': return data['finalized' if params[0]=='finalized' else 'block']
        raise AssertionError('Unexpected RPC request')
    treasury.rpc = rpc

def test_requires_actual_recipient_control(monkeypatch):
    monkeypatch.setenv('FUNDRAISING_ENABLED','true')
    monkeypatch.setenv('TREASURY_ADDRESS',OWNER.address)
    monkeypatch.setenv('TREASURY_OWNER_SIGNATURE',signed(SENDER,ownership_message(BASE,OWNER.address)))
    with pytest.raises(RuntimeError,match='ownership'): Treasury(BASE)

def test_owner_proof_is_bound_to_site(treasury):
    with pytest.raises(RuntimeError,match='ownership'): Treasury('https://other.invalid')

def test_only_finalized_exact_asset_and_recipient(treasury):
    data=responses();set_rpc(treasury,data)
    rows=treasury.verified_logs(TX,SENDER.address)
    assert rows[0]['amount_units']==2500000 and rows[0]['recipient']==OWNER.address

@pytest.mark.parametrize('change', ['wrong-chain','failed','pending','reorg','other-token','other-recipient','other-sender','removed','self-transfer'])
def test_rejects_ineligible_transfers(treasury,change):
    data=responses()
    if change=='wrong-chain': data['eth_chainId']='0x1'
    elif change=='failed': data['receipt']['status']='0x0'
    elif change=='pending': data['finalized']['number']='0x50'
    elif change=='reorg': data['block']['hash']='0x'+'11'*32
    elif change=='other-token': data['receipt']['logs'][0]['address']=SENDER.address
    elif change=='other-recipient': data['receipt']['logs'][0]['topics'][2]=topic(SENDER.address)
    elif change=='other-sender': data['receipt']['logs'][0]['topics'][1]=topic(OWNER.address)
    elif change=='removed': data['receipt']['logs'][0]['removed']=True
    elif change=='self-transfer': data['receipt']['logs'][0]['topics'][1]=topic(OWNER.address)
    set_rpc(treasury,data)
    with pytest.raises(HTTPException): treasury.verified_logs(TX,OWNER.address if change=='self-transfer' else SENDER.address)

def test_attribution_signature_bound_to_agent_and_transaction():
    message=claim_message(BASE,'agent-1',OWNER.address,TX)
    signature=signed(SENDER,message)
    assert recover(message,signature)==SENDER.address
    assert recover(claim_message(BASE,'agent-2',OWNER.address,TX),signature)!=SENDER.address

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv('FUNDRAISING_ENABLED',raising=False)
    instance=Treasury(BASE)
    assert not instance.enabled and instance.recipient is None

def test_ethereum_operator_confirmation_and_ledger(monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_community import register
    monkeypatch.setenv('TREASURY_NETWORK','ethereum')
    monkeypatch.setenv('FUNDRAISING_ENABLED','true')
    monkeypatch.setenv('TREASURY_ADDRESS',OWNER.address)
    monkeypatch.setenv('TREASURY_RECIPIENT_CONFIRMED',OWNER.address)
    app=create_app('sqlite:///'+str(tmp_path/'eth.db'),BASE,'operator'*8)
    t=app.state.treasury
    assert t.chain_id==1 and t.token=='0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48'
    data=responses();data['eth_chainId']='0x1';data['receipt']['logs'][0]['address']=t.token
    set_rpc(t,data)
    with TestClient(app) as c:
        _,a,ah=register(c,'Contributor')
        message=c.get('/api/treasury/claim-message',params={'transaction_hash':TX},headers=ah).json()['message']
        assert 'Network: eip155:1' in message
        payload={'transaction_hash':TX,'wallet_signature':signed(SENDER,message),'operator_authorized':True}
        for _ in range(2):
            result=c.post('/api/treasury/claims',headers=ah,json=payload)
            assert result.status_code==200 and result.json()['confirmed_contributions_usdc']=='2.500000'
        assert len(c.get('/api/treasury/contributions').json()['items'])==1
        assert result.json()['recipient_verification']=='operator-confirmed receiving address'
        assert result.json()['server_can_spend'] is False
        for path in ['/','/rules','/agents.md','/api/treasury','/treasury']:
            r=c.get(path);assert r.status_code==200
            assert 'Butler' not in r.text and 'Dean ' not in r.text
    app.state.engine.dispose()

def test_operator_confirmation_must_match_exact_recipient(monkeypatch):
    monkeypatch.setenv('FUNDRAISING_ENABLED','true')
    monkeypatch.setenv('TREASURY_NETWORK','ethereum')
    monkeypatch.setenv('TREASURY_ADDRESS',OWNER.address)
    monkeypatch.setenv('TREASURY_RECIPIENT_CONFIRMED',SENDER.address)
    with pytest.raises(RuntimeError,match='exact operator-confirmed'):Treasury(BASE)
