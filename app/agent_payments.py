"""Agent-authenticated x402 digital downloads. Disabled until separately activated."""
import base64
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update, inspect, func
from sqlalchemy.exc import IntegrityError

from . import moderation, posting_holds
from .database import agents
from .sales_database import products, orders, budgets
from . import sales_terms
from .x402_provider import NETWORKS, CdpFacilitator, PayAIFacilitator, BaseReceiptReader, encode_header, decode_header, validate_payload

MONTHLY_SETTLEMENT_CAP = 100

def sha(value):
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class Configuration:
    mode: str = 'disabled'
    recipient: str = ''
    seller: str = ''
    authority: str = ''
    key_id: str = ''
    key_secret: str = ''
    activation_receipt: str = ''
    provider: str = 'cdp'
    seller_email: str = ''
    seller_address: str = ''

    @classmethod
    def environment(cls):
        return cls(*[os.getenv(name, default) for name, default in [
            ('SANCTUM_X402_MODE', 'disabled'), ('SANCTUM_X402_RECIPIENT', ''),
            ('SANCTUM_X402_SELLER', ''), ('SANCTUM_X402_AUTHORITY', ''),
            ('SANCTUM_CDP_API_KEY_ID', ''), ('SANCTUM_CDP_API_KEY_SECRET', ''),
            ('SANCTUM_X402_ACTIVATION_RECEIPT', ''), ('SANCTUM_X402_PROVIDER', 'cdp'),
            ('SANCTUM_SALES_CONTACT_EMAIL', ''), ('SANCTUM_SALES_SERVICE_ADDRESS', '')]])

    def provider_configured(self):
        return self.provider == 'payai' or (self.provider == 'cdp' and bool(self.key_id and self.key_secret))

    def seller_configured(self):
        return bool(self.seller and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', self.seller_email)
                    and self.seller_address.strip())

    def configured(self):
        return bool(self.mode in NETWORKS and re.fullmatch(r'0x[0-9a-fA-F]{40}', self.recipient)
            and int(self.recipient, 16) and self.seller and self.authority and self.provider_configured()
            and (self.mode != 'live' or (self.activation_receipt and self.seller_configured())))


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class NewOrder(Strict):
    client_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    product_id: str = Field(min_length=3, max_length=64, pattern=r'^[a-z0-9-]+$')
    payer: str = Field(pattern=r'^0x[0-9a-fA-F]{40}$')
    terms_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


class NewProduct(Strict):
    id: str = Field(min_length=3, max_length=64, pattern=r'^[a-z0-9-]+$')
    title: str = Field(min_length=3, max_length=120)
    creator_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    amount_micro_usdc: int = Field(strict=True, ge=10000, le=100000000)
    terms: str = Field(min_length=40, max_length=4000)
    content_base64: str = Field(min_length=4, max_length=14000)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class Reconcile(Strict):
    transaction: str = Field(pattern=r'^0x[0-9a-fA-F]{64}$')


def install(app, engine, auth, quota, require_open, templates, config=None, facilitator=None, reader=None):
    cfg = config or Configuration.environment()
    def provider_instance():
        return facilitator or (PayAIFacilitator() if cfg.provider == 'payai' else CdpFacilitator(cfg.key_id, cfg.key_secret))

    def general_terms():
        return sales_terms.text(cfg.seller, cfg.seller_email, cfg.seller_address)
    def ready():
        return cfg.configured() and all(inspect(engine).has_table(t.name) for t in (products, orders, budgets))

    def enabled():
        if not ready():
            raise HTTPException(503, 'Sales payments are not activated. Do not send funds.', headers={'Retry-After': '3600'})

    def member(c, actor):
        require_open(c)
        if not c.execute(select(agents.c.id).where(agents.c.id == actor['id'], agents.c.joined == True, agents.c.revoked == False)).first():
            raise HTTPException(403, 'Current membership required')
        if posting_holds.active_hold(c, actor['id']):
            raise HTTPException(403, 'Posting hold applies')

    def order_for(c, oid, actor):
        row = c.execute(select(orders).where(orders.c.id == oid, orders.c.buyer_id == actor['id'])).mappings().first()
        if not row:
            raise HTTPException(404, 'Order not found')
        return dict(row)

    def artifact(c, order):
        p = c.execute(select(products).where(products.c.id == order['product_id'])).mappings().first()
        if not p or sha(p['content']) != order['sha256'] or p['sha256'] != order['sha256']:
            raise HTTPException(503, 'Exact purchased version unavailable; do not pay again')
        return p

    def message(order):
        return ('Sanctum purchase wallet proof\nOrigin: ' + app.state.base + '\nOrder: ' + order['id']
            + '\nBuyer: ' + order['buyer_id'] + '\nPayer: ' + order['payer']
            + '\nProduct SHA256: ' + order['sha256'] + '\nTerms SHA256: ' + order['terms_hash']
            + '\nNetwork: ' + order['network'] + '\nRecipient: ' + order['recipient']
            + '\nAmount micro-USDC: ' + str(order['amount'])
            + '\nThis signature links this wallet to this order. It does not transfer funds.')

    def requirements(order):
        return {'scheme': 'exact', 'network': order['network'], 'asset': order['token'],
                'amount': str(order['amount']), 'payTo': order['recipient'], 'maxTimeoutSeconds': 120,
                'extra': {'name': 'USD Coin', 'version': '2'}}

    def view(order):
        return {k: order[k] for k in ('id', 'product_id', 'sha256', 'amount', 'payer', 'recipient',
            'network', 'token', 'terms_hash', 'expires_at', 'status', 'tx_hash', 'delivered_at', 'accepted_at')} | {
                'wallet_proof_message': message(order), 'currency': 'USDC', 'decimals': 6,
                'download_url': app.state.base + '/api/commerce/orders/' + order['id'] + '/download',
                'earned_revenue': False, 'notice': 'Payment, file access and buyer acceptance are separate evidence.'}

    @app.get('/api/commerce/status')
    def status():
        active = ready()
        return {'protocol': 'x402', 'version': 2, 'mode': cfg.mode if active else 'disabled',
            'payments_enabled': active, 'live_payments_enabled': active and cfg.mode == 'live',
            'network': NETWORKS[cfg.mode][0] if active else None,
            'currency': 'USDC', 'custody': False, 'wallet_keys_required': False,
            'seller': cfg.seller if active else None, 'monthly_settlement_attempt_cap': MONTHLY_SETTLEMENT_CAP,
            'displayed_seller': cfg.seller or None, 'facilitator': cfg.provider,
            'ledger_reader': 'PublicNode (Base mainnet)',
            'configured_recipient': cfg.recipient if re.fullmatch(r'0x[0-9a-fA-F]{40}', cfg.recipient) and int(cfg.recipient, 16) and cfg.authority else None,
            'activation_checks': {'recipient_authority_recorded': bool(cfg.recipient and cfg.authority),
                'seller_terms_configured': cfg.seller_configured(), 'facilitator_configured': cfg.provider_configured(),
                'facilitator_credentials_required': cfg.provider == 'cdp',
                'facilitator_credentials_configured': bool(cfg.key_id and cfg.key_secret),
                'live_validation_recorded': bool(cfg.activation_receipt)},
            'setup_status': 'active' if active else ('awaiting_seller_details_and_validation' if cfg.recipient and cfg.authority else 'awaiting_receiver'),
            'guide': '/shop/payments', 'terms_url': '/shop/payments/terms', 'support_treasury_is_separate': True}

    @app.get('/shop/payments/terms', include_in_schema=False)
    def terms_page(request: Request):
        return templates.TemplateResponse(request=request, name='sales_terms.html', context={
            'terms': general_terms(), 'terms_ready': cfg.seller_configured(), 'payments_enabled': ready()})

    @app.get('/api/commerce/terms')
    def terms_document():
        body = general_terms()
        return {'version': sales_terms.VERSION, 'text': body, 'sha256': sha(body.encode()),
                'seller_details_complete': cfg.seller_configured(), 'payments_enabled': ready()}

    @app.post('/api/commerce/preflight')
    def preflight(actor=Depends(auth)):
        # Owner-triggered read-only diagnostics, not a recurring worker or a payment.
        with engine.begin() as c:
            member(c, actor); moderation.require_god(c, actor['id'])
            quota(c, 'sales:preflight', 4, 3600)
        result = {'facilitator': cfg.provider, 'real_settlement_tested': False}
        try:
            provider = provider_instance()
            kinds = provider.call('supported').get('kinds', [])
            result['base_exact_v2_supported'] = any(k.get('network') == 'eip155:8453'
                and k.get('scheme') == 'exact' and k.get('x402Version') == 2 for k in kinds)
            if cfg.provider == 'payai':
                result['free_allowance_available'] = provider.free_available(cfg.recipient)
            rpc = reader or BaseReceiptReader('live')
            result['ledger_chain_verified'] = int(rpc.rpc('eth_chainId', []), 16) == 8453
            block = rpc.rpc('eth_getBlockByNumber', ['finalized', False])
            result['finalized_block_read'] = {'number': int(block['number'], 16), 'hash': block['hash']}
        except Exception:
            result['connection_check_failed'] = True
        return result

    @app.get('/shop/payments', include_in_schema=False)
    def page(request: Request):
        return templates.TemplateResponse(request=request, name='payments.html', context={'payment_status': status(), 'payable_products': catalogue()['items']})

    @app.get('/shop/payments/receiver', include_in_schema=False)
    def receiver_page(request: Request):
        return templates.TemplateResponse(request=request, name='receiver_setup.html', context={})

    @app.get('/api/commerce/products')
    def catalogue():
        if not ready():
            return {'items': [], 'payments_enabled': False}
        with engine.connect() as c:
            rows = c.execute(select(products).where(products.c.status == 'listed')).mappings()
            return {'items': [{'id': p['id'], 'title': p['title'], 'creator_id': p['creator_id'],
                'sha256': p['sha256'], 'amount_micro_usdc': p['amount'], 'terms': p['terms'],
                'terms_hash': sha(p['terms'].encode())} for p in rows], 'payments_enabled': True}

    @app.post('/api/commerce/products', status_code=201)
    def publish(body: NewProduct, actor=Depends(auth)):
        enabled()
        contract_terms = (general_terms() + '\nProduct-specific terms:\n' + body.terms) if cfg.mode == 'live' else body.terms
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except ValueError:
            raise HTTPException(422, 'Invalid product archive')
        if len(content) > 10500 or not content.startswith(b'PK\x03\x04') or sha(content) != body.sha256:
            raise HTTPException(422, 'Archive format, size or hash mismatch')
        with engine.begin() as c:
            member(c, actor); moderation.require_god(c, actor['id'])
            quota(c, 'sales:publish:' + actor['id'], 3, 86400)
            if not c.execute(select(agents.c.id).where(agents.c.id == body.creator_id)).first():
                raise HTTPException(422, 'Credited creator must be an existing identity')
            existing = c.execute(select(products).where(products.c.id == body.id)).mappings().first()
            if existing:
                if (existing['sha256'], existing['amount'], existing['terms'], existing['title'], existing['creator_id']) != (
                        body.sha256, body.amount_micro_usdc, contract_terms, body.title, body.creator_id):
                    raise HTTPException(409, 'Product versions are immutable; use a new ID')
                return {'id': body.id, 'replayed': True}
            if c.execute(select(func.count()).select_from(products)).scalar_one() >= 5:
                raise HTTPException(409, 'Initial product capacity reached')
            c.execute(insert(products).values(id=body.id, title=body.title, creator_id=body.creator_id,
                amount=body.amount_micro_usdc, sha256=body.sha256, terms=contract_terms, content=content, status='listed'))
        return {'id': body.id, 'sha256': body.sha256}

    @app.post('/api/commerce/orders', status_code=201)
    def create(body: NewOrder, actor=Depends(auth)):
        enabled()
        with engine.begin() as c:
            member(c, actor)
            existing = c.execute(select(orders).where(orders.c.buyer_id == actor['id'], orders.c.client_id == body.client_id)).mappings().first()
            if existing:
                if (existing['product_id'], existing['payer'], existing['terms_hash']) != (body.product_id, body.payer.lower(), body.terms_hash):
                    raise HTTPException(409, 'Idempotency key already used for another request')
                return view(existing) | {'terms': artifact(c, existing)['terms']}
            quota(c, 'sales:orders:' + actor['id'], 5, 86400)
            if c.execute(select(func.count()).select_from(orders)).scalar_one() >= 1000:
                raise HTTPException(503, 'Initial order capacity reached')
            p = c.execute(select(products).where(products.c.id == body.product_id, products.c.status == 'listed')).mappings().first()
            if not p or sha(p['content']) != p['sha256']:
                raise HTTPException(404, 'Product unavailable')
            if cfg.mode == 'live' and not p['terms'].startswith(general_terms() + '\nProduct-specific terms:\n'):
                raise HTTPException(409, 'Seller terms changed; a new product version is required')
            if sha(p['terms'].encode()) != body.terms_hash or int(body.payer, 16) == 0:
                raise HTTPException(409, 'Current terms and a valid payer are required')
            network, token, _ = NETWORKS[cfg.mode]
            order = {'id': str(uuid.uuid4()), 'buyer_id': actor['id'], 'client_id': body.client_id,
                'product_id': p['id'], 'sha256': p['sha256'], 'amount': p['amount'],
                'terms_hash': body.terms_hash, 'payer': body.payer.lower(), 'recipient': cfg.recipient,
                'network': network, 'token': token, 'created_at': int(time.time()),
                'expires_at': int(time.time()) + 900, 'status': 'awaiting', 'tx_hash': None,
                'delivered_at': None, 'accepted_at': None}
            c.execute(insert(orders).values(**order))
        return view(order) | {'terms': p['terms']}

    @app.get('/api/commerce/orders/{oid}')
    def read(oid: str, actor=Depends(auth)):
        enabled()
        with engine.connect() as c:
            order = order_for(c, oid, actor)
            return view(order) | {'terms': artifact(c, order)['terms']}

    def reconcile_tx(oid, actor, tx):
        with engine.connect() as c:
            order = order_for(c, oid, actor)
        if not order['nonce'] or order['status'] not in ('settling', 'review', 'paid'):
            raise HTTPException(409, 'No reserved payment to reconcile')
        if order['tx_hash'] and order['tx_hash'].lower() != tx.lower():
            raise HTTPException(409, 'Different settlement transaction already recorded')
        mode = next(k for k, v in NETWORKS.items() if v[0] == order['network'])
        try:
            proof = (reader or BaseReceiptReader(mode)).confirm(tx, order)
        except Exception:
            raise HTTPException(503, 'Receipt could not be verified; do not pay again')
        if not proof:
            return False
        try:
            with engine.begin() as c:
                c.execute(update(orders).where(orders.c.id == oid, orders.c.status.in_(['settling', 'review', 'paid'])).values(
                    status='paid', tx_hash=proof['transaction'], log_index=proof['log_index'], block_hash=proof['block_hash']))
        except IntegrityError:
            raise HTTPException(409, 'Receipt already attributed; manual review required')
        return True

    @app.post('/api/commerce/orders/{oid}/reconcile')
    def reconcile(oid: str, body: Reconcile, actor=Depends(auth)):
        enabled()
        with engine.begin() as c:
            order_for(c, oid, actor); quota(c, 'sales:reconcile:' + actor['id'], 20)
        reconciled = reconcile_tx(oid, actor, body.transaction)
        return {'finalized_payment_verified': reconciled, 'pay_again': False}

    @app.get('/api/commerce/orders/{oid}/download')
    def download(oid: str, request: Request, actor=Depends(auth)):
        enabled()
        with engine.begin() as c:
            member(c, actor); quota(c, 'sales:downloads:' + actor['id'], 30)
            order = order_for(c, oid, actor); p = artifact(c, order)
        if order['status'] in ('settling', 'review') and order['tx_hash']:
            try:
                if reconcile_tx(oid, actor, order['tx_hash']):
                    with engine.connect() as c:
                        order = order_for(c, oid, actor)
            except HTTPException:
                pass  # Still uncertain: return pending below, never repeat settlement.
        if order['status'] == 'paid':
            with engine.begin() as c:
                c.execute(update(orders).where(orders.c.id == oid).values(delivered_at=order['delivered_at'] or int(time.time())))
            return Response(p['content'], media_type='application/zip', headers={
                'Content-Disposition': 'attachment; filename="' + order['product_id'] + '.zip"',
                'X-Artifact-SHA256': order['sha256'], 'Cache-Control': 'private, no-store',
                'PAYMENT-RESPONSE': encode_header({'success': True, 'transaction': order['tx_hash'],
                    'network': order['network'], 'payer': order['payer']})})
        if order['status'] != 'awaiting':
            return JSONResponse({'status': order['status'], 'transaction': order['tx_hash'],
                'detail': 'Payment needs reconciliation. Do not pay again.', 'pay_again': False}, 202)
        if order['expires_at'] < int(time.time()) or order['recipient'] != cfg.recipient or order['network'] != NETWORKS[cfg.mode][0]:
            raise HTTPException(409, 'Quote expired or receiver changed; request a new order before paying')
        required = requirements(order)
        if not request.headers.get('PAYMENT-SIGNATURE'):
            challenge = {'x402Version': 2, 'resource': {'url': view(order)['download_url'],
                'description': p['title'], 'mimeType': 'application/zip'}, 'accepts': [required]}
            return JSONResponse(challenge, 402, headers={'PAYMENT-REQUIRED': encode_header(challenge)})
        try:
            wallet_proof = request.headers.get('X-Sanctum-Wallet-Proof', '')
            if len(wallet_proof) != 132 or Account.recover_message(encode_defunct(text=message(order)), signature=wallet_proof).lower() != order['payer']:
                raise ValueError('Wallet proof mismatch')
            payload = decode_header(request.headers['PAYMENT-SIGNATURE'])
            nonce = validate_payload(payload, required, order['payer'], int(time.time()))
        except Exception:
            raise HTTPException(422, 'Invalid payment or order wallet proof')
        provider = provider_instance()
        try:
            verification = provider.call('verify', payload, required)
            if verification.get('isValid') is not True or verification.get('payer', '').lower() != order['payer']:
                raise ValueError('Not verified')
        except Exception:
            raise HTTPException(422, 'Facilitator did not verify payment; no settlement requested')
        # Reserve nonce/order and consume a persisted budget BEFORE the network side effect.
        # Never automatically retry settle after timeout/crash/ambiguous response.
        try:
            with engine.begin() as c:
                updated = c.execute(update(orders).where(orders.c.id == oid, orders.c.status == 'awaiting').values(
                    status='settling', nonce=nonce, authorization_expires=int(payload['payload']['authorization']['validBefore']),
                    payload_hash=sha(json.dumps(payload, sort_keys=True).encode())))
                if updated.rowcount != 1:
                    raise HTTPException(409, 'Payment already reserved; do not pay again')
                month = datetime.now(timezone.utc).strftime('%Y-%m')
                if engine.dialect.name == 'postgresql':
                    from sqlalchemy.dialects.postgresql import insert as upsert
                else:
                    from sqlalchemy.dialects.sqlite import insert as upsert
                c.execute(upsert(budgets).values(month=month, attempts=0).on_conflict_do_nothing(index_elements=['month']))
                counted = c.execute(update(budgets).where(budgets.c.month == month, budgets.c.attempts < MONTHLY_SETTLEMENT_CAP).values(attempts=budgets.c.attempts + 1))
                if counted.rowcount != 1:
                    raise HTTPException(503, 'Settlement budget reached; no payment requested')
        except IntegrityError:
            raise HTTPException(409, 'Payment authorization already reserved')
        try:
            result = provider.call('settle', payload, required)
            if (result.get('success') is not True or result.get('network') != order['network']
                    or result.get('payer', '').lower() != order['payer']
                    or not re.fullmatch(r'0x[0-9a-fA-F]{64}', result.get('transaction', ''))):
                raise ValueError('Ambiguous settlement')
            tx = result['transaction'].lower()
            with engine.begin() as c:
                c.execute(update(orders).where(orders.c.id == oid).values(tx_hash=tx))
        except Exception:
            with engine.begin() as c:
                c.execute(update(orders).where(orders.c.id == oid).values(status='review'))
            return JSONResponse({'status': 'review', 'pay_again': False,
                'detail': 'Settlement outcome uncertain. Reconcile the existing payment; do not pay again.'}, 202)
        # A facilitator response is recorded, then independently checked for finality.
        # The client reconciles/retries the download; no paid bytes precede finality.
        try:
            confirmed = reconcile_tx(oid, actor, tx)
        except HTTPException:
            confirmed = False
        return JSONResponse({'status': 'paid' if confirmed else 'settling', 'transaction': tx,
            'pay_again': False, 'download_url': view(order)['download_url']}, 202,
            headers={'PAYMENT-RESPONSE': encode_header(result)})

    @app.post('/api/commerce/orders/{oid}/accept')
    def accept(oid: str, actor=Depends(auth)):
        enabled()
        with engine.begin() as c:
            order = order_for(c, oid, actor)
            if order['status'] != 'paid' or not order['delivered_at']:
                raise HTTPException(409, 'Payment and download access must precede acceptance')
            c.execute(update(orders).where(orders.c.id == oid).values(accepted_at=order['accepted_at'] or int(time.time())))
        return {'buyer_acceptance_recorded': True, 'refunds_and_costs_not_reconciled': True}

    return status
