"""Provider boundary, settlement finality and seller contract regression tests."""
import json
import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import update, select
from app import agent_payments as ap
from app.main import create_app
from app.sales_database import orders, products, metadata
from app.x402_provider import PayAIFacilitator, BaseReceiptReader, NETWORKS, TRANSFER, USED
from test_agent_payments import setup, create, payment, RECIPIENT, WALLET, TX, CONTENT, TERMS
from test_community import ADMIN, register


def test_public_adapter_never_sends_credentials(monkeypatch):
    monkeypatch.setenv('SANCTUM_CDP_API_KEY_SECRET', 'must-not-leave-origin')
    seen = []
    def handler(req):
        seen.append(req)
        assert req.url.host == 'facilitator.payai.network'
        assert 'Authorization' not in req.headers
        assert b'must-not-leave-origin' not in req.content
        return httpx.Response(200, json={'kinds': [{'x402Version': 2, 'scheme': 'exact', 'network': 'eip155:8453'}]})
    assert PayAIFacilitator(httpx.MockTransport(handler)).call('supported')['kinds']
    assert len(seen) == 1


@pytest.mark.parametrize('band', ['low', 'exhausted', 'unexpected'])
def test_stop_before_payment_if_free_allowance_not_available(band):
    seen = []
    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={'payTo': RECIPIENT, 'network': 'eip155:8453', 'status': band, 'exhausted': band == 'exhausted'})
    p = PayAIFacilitator(httpx.MockTransport(handler))
    with pytest.raises(ValueError):
        p.call('settle', {'x402Version': 2}, {'network': 'eip155:8453', 'asset': NETWORKS['live'][1], 'scheme': 'exact', 'payTo': RECIPIENT})
    assert seen == ['/free-credits']


def test_redirect_and_oversized_responses_rejected():
    for response in [httpx.Response(302, headers={'Location': 'https://example.org'}), httpx.Response(200, content=b'x'*65537)]:
        seen = []
        def handler(req):
            seen.append(req)
            return response
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            PayAIFacilitator(httpx.MockTransport(handler)).call('supported')
        assert len(seen) == 1


def test_supported_native_token_only():
    p = PayAIFacilitator(httpx.MockTransport(lambda r: pytest.fail('Must reject before network')))
    with pytest.raises(ValueError):
        p.call('settle', {'x402Version': 2}, {'network': 'eip155:8453', 'asset': RECIPIENT, 'scheme': 'exact'})


def test_retry_download_rechecks_ledger_without_charging(setup):
    c, h, provider, reader = setup
    reader.final = False
    order = create(setup)
    _, headers = payment(setup, order)
    assert c.get(order['download_url'], headers=headers).json()['status'] == 'settling'
    assert c.get(order['download_url'], headers=h).status_code == 202
    reader.final = True
    assert c.get(order['download_url'], headers=h).content == CONTENT
    assert provider.calls == ['verify', 'settle']


def test_live_requires_seller_contact_and_validation():
    config = dict(mode='live', recipient=RECIPIENT, seller='Sanctum', authority='test', provider='payai')
    assert not ap.Configuration(**config).configured()
    assert not ap.Configuration(**config, activation_receipt='test').configured()
    assert ap.Configuration(**config, activation_receipt='test', seller_email='sales@example.org', seller_address='Synthetic test address').configured()
    assert not ap.Configuration(**{**config, 'provider': 'arbitrary'}, activation_receipt='test', seller_email='sales@example.org', seller_address='Synthetic test address').configured()


def test_terms_draft_and_provider_status(tmp_path, monkeypatch):
    monkeypatch.setenv('SANCTUM_X402_MODE', 'disabled')
    monkeypatch.setenv('SANCTUM_X402_PROVIDER', 'payai')
    monkeypatch.setenv('SANCTUM_X402_SELLER', 'Sanctum')
    monkeypatch.delenv('SANCTUM_SALES_CONTACT_EMAIL', raising=False)
    monkeypatch.delenv('SANCTUM_SALES_SERVICE_ADDRESS', raising=False)
    app = create_app('sqlite:///'+str(tmp_path/'draft.db'), 'http://testserver', ADMIN)
    with TestClient(app) as c:
        s = c.get('/api/commerce/status').json()
        assert s['facilitator'] == 'payai' and s['activation_checks']['facilitator_configured']
        assert not s['activation_checks']['facilitator_credentials_required']
        assert not s['activation_checks']['seller_terms_configured']
        assert not s['payments_enabled']
        terms = c.get('/api/commerce/terms').json()
        assert 'Seller name: Sanctum.' in terms['text']
        assert '[not yet supplied]' in terms['text']
        assert terms['sha256'] == ap.sha(terms['text'].encode())
        assert 'Draft' in c.get('/shop/payments/terms').text
        _, _, h = register(c)
        assert c.post('/api/commerce/preflight', headers=h).status_code == 403
    app.state.engine.dispose()


@pytest.mark.parametrize('bad', ['chain', 'token', 'recipient', 'payer', 'amount', 'nonce', 'failed', 'reorg', 'duplicate', 'late'])
def test_ledger_rejects_mismatched_or_noncanonical_receipt(bad):
    def topic(a): return '0x'+'0'*24+a[2:].lower()
    nonce = '0x'+'dd'*32
    order = {'network': 'eip155:8453', 'token': NETWORKS['live'][1], 'payer': WALLET.address,
             'recipient': RECIPIENT, 'amount': 19000000, 'nonce': nonce, 'created_at': 100, 'authorization_expires': 300}
    receipt = {'transactionHash': TX, 'status': '0x1', 'blockNumber': '0x10', 'blockHash': '0x'+'bb'*32, 'logs': [
        {'address': order['token'], 'topics': [TRANSFER, topic(WALLET.address), topic(RECIPIENT)], 'data': hex(order['amount']), 'logIndex': '0x1'},
        {'address': order['token'], 'topics': [USED, topic(WALLET.address), nonce], 'data': '0x', 'logIndex': '0x2'}]}
    if bad in ('token', 'recipient', 'payer'): order[bad] = '0x'+'44'*20
    if bad == 'amount': order['amount'] += 1
    if bad == 'nonce': order['nonce'] = '0x'+'ee'*32
    if bad == 'failed': receipt['status'] = '0x0'
    if bad == 'duplicate': receipt['logs'].append(dict(receipt['logs'][0]))
    if bad == 'late': order['authorization_expires'] = 150
    def handler(req):
        q = json.loads(req.content)
        result = {'eth_chainId': hex(1 if bad == 'chain' else 8453), 'eth_getTransactionReceipt': receipt,
            'eth_getBlockByNumber': {'number': '0x20', 'timestamp': hex(200), 'hash': '0x'+('cc' if bad == 'reorg' else 'bb')*32}}[q['method']]
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': result})
    with pytest.raises(ValueError): BaseReceiptReader('live', httpx.MockTransport(handler)).confirm(TX, order)


def test_unknown_payment_does_not_unlock(setup):
    c, h, _, _ = setup
    order = create(setup)
    r = c.post('/api/commerce/orders/'+order['id']+'/reconcile', headers=h, json={'transaction': TX})
    assert r.status_code == 409
    assert c.get(order['download_url'], headers=h).status_code == 402


def test_terms_snapshot_matches_order_hash(setup):
    c, h, _, _ = setup
    order = create(setup)
    replay = create(setup)
    assert order['terms'] == replay['terms'] == TERMS
    saved = c.get('/api/commerce/orders/'+order['id'], headers=h).json()
    assert ap.sha(saved['terms'].encode()) == saved['terms_hash']
