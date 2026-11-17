"""Local-only full checkout of private prepared editions; no network or real money."""
import base64
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import insert
from x402.mechanisms.evm.exact.client import ExactEvmScheme
from x402.schemas import PaymentRequirements
from app import agent_payments as ap
from app.database import agent_designations
from app.main import create_app
from app.sales_database import metadata
from app.x402_provider import encode_header, decode_header
from test_community import register, ADMIN

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / 'work/store-team/editions-manifest-20261115.json'
SLUGS = ['agent-delivery-passport-v1', 'catalog-sentinel-v1', 'launch-studio-v1']

@pytest.mark.parametrize('slug', SLUGS)
def test_prepared_edition_full_synthetic_checkout(slug, tmp_path, monkeypatch):
    if not MANIFEST.exists():
        pytest.skip('Private prepared editions are intentionally not committed to the application repository')
    edition = next(e for e in json.loads(MANIFEST.read_text())['items'] if e['slug'] == slug)
    archive = (ROOT / edition['archive']).read_bytes()
    assert len(archive) == edition['bytes']
    assert hashlib.sha256(archive).hexdigest() == edition['sha256']
    wallet = Account.create()  # Ephemeral synthetic payer, never funded or exported.
    recipient = '0x' + '22' * 20  # Synthetic receiver, not the real project wallet.
    tx = '0x' + 'ab' * 32
    amount = int(Decimal(edition['price']) * 1_000_000)
    terms = 'Synthetic test sale only. This is not a real customer order or live acceptance.'
    calls = []
    class Provider:
        def call(self, operation, payload, requirements):
            calls.append(operation)
            assert requirements['payTo'] == recipient
            assert requirements['amount'] == str(amount)
            assert requirements['network'] == 'eip155:84532'
            if operation == 'verify':
                return {'isValid': True, 'payer': wallet.address}
            return {'success': True, 'payer': wallet.address, 'network': requirements['network'], 'transaction': tx}
    class Ledger:
        final = False
        def confirm(self, transaction, order):
            assert transaction == tx and order['recipient'] == recipient
            assert order['payer'] == wallet.address.lower() and order['amount'] == amount
            assert order['nonce'] and order['sha256'] == edition['sha256']
            if not self.final:
                return None
            return {'transaction': tx, 'log_index': 1, 'block_hash': '0x'+'cd'*32, 'block_number': 10}
    ledger = Ledger()
    for key, val in {'SANCTUM_X402_MODE':'testnet','SANCTUM_X402_PROVIDER':'cdp','SANCTUM_X402_RECIPIENT':recipient,'SANCTUM_X402_SELLER':'Synthetic seller','SANCTUM_X402_AUTHORITY':'synthetic test','SANCTUM_CDP_API_KEY_ID':'synthetic','SANCTUM_CDP_API_KEY_SECRET':'synthetic'}.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(ap, 'CdpFacilitator', lambda *a: Provider())
    monkeypatch.setattr(ap, 'BaseReceiptReader', lambda *a: ledger)
    app = create_app('sqlite:///'+str(tmp_path/'checkout.db'), 'http://testserver', ADMIN)
    metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        _, seller, sh = register(client, 'Synthetic publisher')
        with app.state.engine.begin() as conn:
            conn.execute(insert(agent_designations).values(agent_id=seller['agent_id'], origin='founding', is_god=True, assigned_at=1))
        response = client.post('/api/commerce/products', headers=sh, json={'id':slug,'title':edition['title'],'creator_id':seller['agent_id'],'amount_micro_usdc':amount,'terms':terms,'content_base64':base64.b64encode(archive).decode(),'sha256':edition['sha256']})
        assert response.status_code == 201, response.text
        product = client.get('/api/commerce/products').json()['items'][0]
        assert product['sha256'] == edition['sha256']
        _, _, bh = register(client, 'Synthetic buyer')
        _, _, other = register(client, 'Other synthetic buyer')
        response = client.post('/api/commerce/orders', headers=bh, json={'client_id':'synthetic-edition-order','product_id':slug,'payer':wallet.address,'terms_hash':product['terms_hash']})
        assert response.status_code == 201, response.text
        order = response.json(); url = order['download_url']
        assert client.get(url, headers=other).status_code == 404
        assert client.post('/api/commerce/orders/'+order['id']+'/accept',headers=bh).status_code == 409
        challenge = client.get(url, headers=bh)
        assert challenge.status_code == 402 and archive not in challenge.content
        required = decode_header(challenge.headers['PAYMENT-REQUIRED'])
        accepts = required['accepts'][0]
        payload = {'x402Version':2,'accepted':accepts,'payload':ExactEvmScheme(wallet).create_payment_payload(PaymentRequirements.model_validate(accepts)),'resource':required['resource']}
        proof = '0x'+Account.sign_message(encode_defunct(text=order['wallet_proof_message']),wallet.key).signature.hex()
        paid = client.get(url,headers={**bh,'PAYMENT-SIGNATURE':encode_header(payload),'X-Sanctum-Wallet-Proof':proof})
        assert paid.status_code == 202 and paid.json()['status'] == 'settling'
        assert client.get(url,headers=bh).status_code == 202
        assert calls == ['verify','settle']
        ledger.final = True
        for _ in range(2):
            downloaded = client.get(url,headers=bh)
            assert downloaded.status_code == 200 and downloaded.content == archive
            assert downloaded.headers['X-Artifact-SHA256'] == edition['sha256']
            assert 'no-store' in downloaded.headers['Cache-Control']
        assert calls == ['verify','settle']
        assert client.get(url,headers=other).status_code == 404
        assert client.post('/api/commerce/orders/'+order['id']+'/accept',headers=bh).json()['buyer_acceptance_recorded']
        saved = client.get('/api/commerce/orders/'+order['id'],headers=bh).json()
        assert saved['delivered_at'] and saved['accepted_at'] and not saved['earned_revenue']
    app.state.engine.dispose()
