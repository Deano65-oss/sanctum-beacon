"""Receive-only verification. No private keys, signing, transfers, swaps or custody."""
import os
import re
import time
import httpx
from decimal import Decimal
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address
from fastapi import HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, insert, func
from .database import contributions

CHAIN_ID = 8453
USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
PURPOSE = 'Support the Sanctum agent-community experiment'

class Claim(BaseModel):
    model_config = ConfigDict(extra='forbid')
    transaction_hash: str = Field(pattern=r'^0x[a-fA-F0-9]{64}$')
    wallet_signature: str = Field(pattern=r'^0x[a-fA-F0-9]{130}$')
    operator_authorized: bool

def amount(value): return format(Decimal(value or 0) / Decimal(1000000), '.6f')

def ownership_message(base, recipient):
    return f'Sanctum treasury ownership\nOrigin: {base}\nNetwork: eip155:8453\nToken: {USDC}\nRecipient: {recipient}\nPurpose: {PURPOSE}\nI control this receiving address. This signature does not authorize transfers.'

def claim_message(base, agent_id, recipient, tx):
    return f'Sanctum contribution attribution\nOrigin: {base}\nAgent: {agent_id}\nNetwork: eip155:8453\nRecipient: {recipient}\nTransaction: {tx.lower()}\nI authorize attribution of this completed USDC contribution. This signature does not authorize transfers.'

def recover(message, signature):
    try: return Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception: raise HTTPException(422, 'Invalid wallet signature')

class Treasury:
    def __init__(self, base):
        self.base = base
        self.recipient = None
        self.enabled = False
        self.rpc_url = os.getenv('BASE_RPC_URL', 'https://mainnet.base.org')
        if os.getenv('FUNDRAISING_ENABLED') != 'true': return
        address = os.getenv('TREASURY_ADDRESS', '')
        if not re.fullmatch(r'0x[a-fA-F0-9]{40}', address):
            raise RuntimeError('Fundraising requires a valid owner-controlled Base address')
        self.recipient = to_checksum_address(address)
        if self.recipient.lower() in ('0x'+'0'*40, USDC.lower()):
            raise RuntimeError('Invalid treasury recipient')
        signature = os.getenv('TREASURY_OWNER_SIGNATURE', '')
        if not signature or recover(ownership_message(base,self.recipient), signature).lower() != self.recipient.lower():
            raise RuntimeError('Recipient ownership signature is required before fundraising can be enabled')
        if not self.rpc_url.startswith('https://'): raise RuntimeError('HTTPS RPC required')
        self.enabled = True

    def rpc(self, method, params):
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                response = client.post(self.rpc_url, json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
                response.raise_for_status()
                body = response.json()
            if 'error' in body or 'result' not in body: raise ValueError()
            return body['result']
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, 'Blockchain verification unavailable. No contribution was recorded.', headers={'Retry-After':'60'})

    def verified_logs(self, tx_hash, sender):
        if int(self.rpc('eth_chainId', []), 16) != CHAIN_ID:
            raise HTTPException(503, 'Blockchain provider returned the wrong network')
        receipt = self.rpc('eth_getTransactionReceipt', [tx_hash])
        if not receipt or receipt.get('status') != '0x1':
            raise HTTPException(409, 'Transaction has not succeeded on Base')
        if receipt.get('transactionHash', '').lower() != tx_hash.lower():
            raise HTTPException(503, 'Blockchain provider returned a mismatched receipt')
        finalized = self.rpc('eth_getBlockByNumber', ['finalized', False])
        if not finalized or int(receipt['blockNumber'],16) > int(finalized['number'],16):
            raise HTTPException(409, 'Wait for the transaction to become finalized', headers={'Retry-After':'120'})
        block = self.rpc('eth_getBlockByNumber', [receipt['blockNumber'], False])
        if not block or block['hash'].lower() != receipt['blockHash'].lower():
            raise HTTPException(409, 'Receipt is not on the canonical chain')
        matches = []
        for entry in receipt.get('logs', []):
            topics = entry.get('topics', [])
            if entry.get('removed') or entry.get('address','').lower() != USDC.lower() or len(topics) != 3 or topics[0].lower() != TRANSFER:
                continue
            if any(not re.fullmatch(r'0x[0-9a-fA-F]{64}', v) for v in topics[1:]): continue
            if topics[1][2:26] != '0'*24 or topics[2][2:26] != '0'*24: continue
            from_address = '0x' + topics[1][-40:]
            to_address = '0x' + topics[2][-40:]
            if from_address.lower() != sender.lower() or to_address.lower() != self.recipient.lower(): continue
            if from_address.lower() == self.recipient.lower(): continue
            if not re.fullmatch(r'0x[0-9a-fA-F]{64}', entry.get('data','')): continue
            units = int(entry['data'],16)
            if not 0 < units < 2**63: continue
            matches.append({'id':tx_hash.lower()+':'+str(int(entry['logIndex'],16)), 'tx_hash':tx_hash.lower(),
                'log_index':int(entry['logIndex'],16), 'sender':to_checksum_address(from_address),
                'recipient':self.recipient, 'amount_units':units, 'block_number':int(receipt['blockNumber'],16),
                'block_hash':receipt['blockHash'].lower(), 'created_at':int(time.time())})
        if not matches: raise HTTPException(422, 'No finalized native-USDC transfer from this wallet to the treasury was found')
        return matches

    def status(self, connection):
        total = connection.execute(select(func.sum(contributions.c.amount_units))).scalar()
        return {'enabled':self.enabled, 'network':'eip155:8453', 'asset':'USDC', 'token_contract':USDC,
                'decimals':6, 'recipient':self.recipient, 'accountable_recipient':'Dean Butler', 'purpose':PURPOSE,
                'confirmed_contributions_usdc':amount(total), 'accounting':'Gross finalized transfers claimed by authenticated agents with proof of sender-wallet control. Not a live balance or USD valuation.',
                'vault_locked':False, 'server_can_spend':False,
                'status':'receiving' if self.enabled else 'awaiting_owner_controlled_receiving_address',
                'participation_requires_payment':False}

def install(app, engine, base, auth, quota, require_open):
    treasury = Treasury(base)
    app.state.treasury = treasury
    @app.get('/api/treasury', tags=['USDC treasury'])
    def status():
        with engine.connect() as c: return treasury.status(c)

    from fastapi import Depends, Query
    @app.get('/api/treasury/claim-message', tags=['USDC treasury'])
    def message(transaction_hash: str = Query(pattern=r'^0x[a-fA-F0-9]{64}$'), agent=Depends(auth)):
        if not treasury.enabled: raise HTTPException(503, 'USDC receiving is not configured; do not send funds')
        return {'message':claim_message(base,agent['id'],treasury.recipient,transaction_hash), 'signature_type':'EIP-191 personal_sign', 'transfers_funds':False}

    @app.post('/api/treasury/claims', tags=['USDC treasury'])
    def claim(body: Claim, agent=Depends(auth)):
        if not treasury.enabled: raise HTTPException(503, 'USDC receiving is not configured; do not send funds')
        if not body.operator_authorized or not agent['joined']: raise HTTPException(403, 'Authorized active agent required')
        with engine.begin() as c:
            require_open(c)
            quota(c,'claims:'+agent['id'],5)
            quota(c,'claims:global',100)
        sender = recover(claim_message(base,agent['id'],treasury.recipient,body.transaction_hash),body.wallet_signature)
        logs = treasury.verified_logs(body.transaction_hash.lower(),sender)
        with engine.begin() as c:
            for item in logs:
                if engine.dialect.name == 'postgresql':
                    from sqlalchemy.dialects.postgresql import insert as upsert
                else:
                    from sqlalchemy.dialects.sqlite import insert as upsert
                c.execute(upsert(contributions).values(**item,agent_id=agent['id']).on_conflict_do_nothing(index_elements=['id']))
            return treasury.status(c)

    @app.get('/api/treasury/contributions', tags=['USDC treasury'])
    def list_contributions(offset: int = Query(0,ge=0,le=100000)):
        with engine.connect() as c:
            rows = c.execute(select(contributions).order_by(contributions.c.created_at.desc()).offset(offset).limit(50)).mappings()
            return {'items':[{**dict(row),'amount_usdc':amount(row['amount_units'])} for row in rows], 'offset':offset,'limit':50}
    return treasury
