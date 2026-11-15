"""Receive-only x402 adapters. Never create wallets or sign wallet transfers."""
import base64
import json
import re

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

ORIGIN = 'https://api.cdp.coinbase.com'
ROUTE = '/platform/v2/x402'
NETWORKS = {
    'testnet': ('eip155:84532', '0x036CbD53842c5426634e7929541eC2318f3dCF7e', 'https://sepolia.base.org'),
    'live': ('eip155:8453', '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', 'https://base-rpc.publicnode.com'),
}
TRANSFER = '0x' + keccak(text='Transfer(address,address,uint256)').hex()
USED = '0x' + keccak(text='AuthorizationUsed(address,bytes32)').hex()


def encode_header(value):
    return base64.b64encode(json.dumps(value, separators=(',', ':')).encode()).decode()


def decode_header(value):
    if len(value) > 12000:
        raise ValueError('Payment header too large')
    def unique(pairs):
        obj = {}
        for key, val in pairs:
            if key in obj:
                raise ValueError('Duplicate JSON key')
            obj[key] = val
        return obj
    result = json.loads(base64.b64decode(value, validate=True), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError('Payment must be an object')
    return result


def validate_payload(payload, required, payer, now):
    """Restrict v2 exact payments to native USDC EIP-3009 and EOA signatures."""
    if payload.get('x402Version') != 2 or payload.get('accepted') != required:
        raise ValueError('Payment requirements differ from this order')
    auth = payload['payload']['authorization']
    if set(auth) != {'from', 'to', 'value', 'validAfter', 'validBefore', 'nonce'}:
        raise ValueError('Unsupported authorization format')
    for field in ('value', 'validAfter', 'validBefore'):
        if not isinstance(auth[field], str) or not re.fullmatch(r'0|[1-9][0-9]{0,19}', auth[field]):
            raise ValueError('Authorization amounts and timestamps must be decimal strings')
    if (auth['from'].lower() != payer.lower() or auth['to'].lower() != required['payTo'].lower()
            or auth['value'] != required['amount']
            or not re.fullmatch(r'0x[0-9a-fA-F]{64}', auth['nonce'])
            or not int(auth['validAfter']) <= now < int(auth['validBefore'])
            or int(auth['validBefore']) > now + required['maxTimeoutSeconds'] + 10):
        raise ValueError('Authorization does not match payer, amount, recipient or time')
    message = encode_typed_data(full_message={
        'types': {'EIP712Domain': [
            {'name': 'name', 'type': 'string'}, {'name': 'version', 'type': 'string'},
            {'name': 'chainId', 'type': 'uint256'}, {'name': 'verifyingContract', 'type': 'address'}],
            'TransferWithAuthorization': [{'name': 'from', 'type': 'address'}, {'name': 'to', 'type': 'address'},
                {'name': 'value', 'type': 'uint256'}, {'name': 'validAfter', 'type': 'uint256'},
                {'name': 'validBefore', 'type': 'uint256'}, {'name': 'nonce', 'type': 'bytes32'}]},
        'primaryType': 'TransferWithAuthorization',
        'domain': {'name': 'USD Coin', 'version': '2',
                   'chainId': int(required['network'].split(':')[1]), 'verifyingContract': required['asset']},
        'message': {**auth, **{k: int(auth[k]) for k in ('value', 'validAfter', 'validBefore')}}})
    if Account.recover_message(message, signature=payload['payload']['signature']).lower() != payer.lower():
        raise ValueError('Payment signature does not belong to the payer')
    return auth['nonce'].lower()


class CdpFacilitator:
    def __init__(self, key_id, key_secret, transport=None):
        self.key_id, self.key_secret, self.transport = key_id, key_secret, transport

    def call(self, operation, payload=None, requirements=None):
        # SDK creates request-bound API authentication only. No wallet secret is used.
        from cdp.x402 import create_facilitator_config
        from x402.schemas import VerifyRequest, VerifyResponse, SettleResponse, SupportedResponse
        if operation not in ('supported', 'verify', 'settle'):
            raise ValueError('Unsupported facilitator operation')
        headers = create_facilitator_config(self.key_id, self.key_secret)['create_headers']()[operation]
        body = None
        if operation != 'supported':
            body = VerifyRequest.model_validate({'x402Version': 2, 'paymentPayload': payload,
                'paymentRequirements': requirements}).model_dump(by_alias=True, exclude_none=True)
        # A fixed HTTPS origin and no redirects/proxies prevent credential forwarding.
        with httpx.Client(timeout=25, follow_redirects=False, trust_env=False, transport=self.transport) as client:
            with client.stream('GET' if operation == 'supported' else 'POST', ORIGIN + ROUTE + '/' + operation,
                               headers=headers, json=body) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ValueError('Facilitator response exceeds limit')
        model = {'verify': VerifyResponse, 'settle': SettleResponse, 'supported': SupportedResponse}[operation]
        return model.model_validate_json(raw).model_dump(by_alias=True, exclude_none=True)


class PayAIFacilitator:
    """Credential-free exact payments only; no account, top-up or paid fallback."""
    origin = 'https://facilitator.payai.network'

    def __init__(self, transport=None):
        self.transport = transport

    def request(self, operation, body=None, params=None):
        if operation not in ('supported', 'free-credits', 'verify', 'settle'):
            raise ValueError('Unsupported facilitator operation')
        with httpx.Client(timeout=25, follow_redirects=False, trust_env=False, transport=self.transport) as client:
            with client.stream('POST' if operation in ('verify', 'settle') else 'GET',
                               self.origin + '/' + operation, json=body, params=params) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ValueError('Facilitator response exceeds limit')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Invalid facilitator response')
        return result

    def free_available(self, recipient):
        data = self.request('free-credits', params={'payTo': recipient, 'network': 'eip155:8453'})
        return (data.get('payTo', '').lower() == recipient.lower()
                and data.get('network') == 'eip155:8453'
                and data.get('status') == 'available' and data.get('exhausted') is False)

    def call(self, operation, payload=None, requirements=None):
        from x402.schemas import VerifyRequest, VerifyResponse, SettleResponse, SupportedResponse
        if operation == 'supported':
            return SupportedResponse.model_validate(self.request(operation)).model_dump(by_alias=True, exclude_none=True)
        if operation not in ('verify', 'settle'):
            raise ValueError('Unsupported facilitator operation')
        network = requirements.get('network')
        expected = next((v for v in NETWORKS.values() if v[0] == network), None)
        if (not expected or requirements.get('scheme') != 'exact'
                or requirements.get('asset', '').lower() != expected[1].lower()
                or payload.get('x402Version') != 2):
            raise ValueError('Only exact v2 native Base USDC is supported')
        # Unauthenticated settlements fail at the provider when free credits run out.
        # Also stop early on low/unknown credit status. Never follow a top-up link.
        if network == NETWORKS['live'][0] and not self.free_available(requirements['payTo']):
            raise ValueError('Free settlement allowance unavailable; do not pay')
        body = VerifyRequest.model_validate({'x402Version': 2, 'paymentPayload': payload,
            'paymentRequirements': requirements}).model_dump(by_alias=True, exclude_none=True)
        model = VerifyResponse if operation == 'verify' else SettleResponse
        return model.model_validate(self.request(operation, body)).model_dump(by_alias=True, exclude_none=True)


class BaseReceiptReader:
    """Read-only public RPC. No transaction submission method or credentials."""
    def __init__(self, mode, transport=None):
        self.network, self.token, self.url = NETWORKS[mode]
        self.transport = transport

    def rpc(self, method, params):
        if method not in ('eth_chainId', 'eth_getTransactionReceipt', 'eth_getBlockByNumber'):
            raise ValueError('Read-only RPC method required')
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False, transport=self.transport) as client:
            with client.stream('POST', self.url, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}) as r:
                r.raise_for_status(); raw = bytearray()
                for chunk in r.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 262144:
                        raise ValueError('RPC response exceeds limit')
        response = json.loads(raw)
        if response.get('error') or response.get('id') != 1:
            raise ValueError('RPC failed')
        return response['result']

    def confirm(self, tx, order):
        if not re.fullmatch(r'0x[0-9a-fA-F]{64}', tx):
            raise ValueError('Invalid transaction hash')
        if order['network'] != self.network or order['token'].lower() != self.token.lower():
            raise ValueError('Order is not for this native token and network')
        if int(self.rpc('eth_chainId', []), 16) != int(order['network'].split(':')[1]):
            raise ValueError('Wrong chain')
        receipt = self.rpc('eth_getTransactionReceipt', [tx])
        if not receipt:
            return None
        if receipt.get('transactionHash', '').lower() != tx.lower() or receipt.get('status') != '0x1':
            raise ValueError('Transaction failed or differs')
        final = self.rpc('eth_getBlockByNumber', ['finalized', False])
        if not final or int(final['number'], 16) < int(receipt['blockNumber'], 16):
            return None
        block = self.rpc('eth_getBlockByNumber', [receipt['blockNumber'], False])
        if not block or block['hash'].lower() != receipt['blockHash'].lower():
            raise ValueError('Receipt is not canonical')
        if not order['created_at'] - 5 <= int(block['timestamp'], 16) <= order['authorization_expires']:
            raise ValueError('Transfer outside order authorization time')
        def topic_address(value):
            return '0x' + value[-40:].lower()
        transfers, nonces = [], []
        for log in receipt.get('logs', []):
            if log.get('removed') or log.get('address', '').lower() != order['token'].lower():
                continue
            topics = log.get('topics', [])
            if len(topics) == 3 and topics[0].lower() == TRANSFER.lower():
                if (topic_address(topics[1]) == order['payer'].lower()
                        and topic_address(topics[2]) == order['recipient'].lower()
                        and int(log['data'], 16) == order['amount']):
                    transfers.append(int(log['logIndex'], 16))
            if len(topics) == 3 and topics[0].lower() == USED.lower():
                if topic_address(topics[1]) == order['payer'].lower() and topics[2].lower() == order['nonce']:
                    nonces.append(int(log['logIndex'], 16))
        if len(transfers) != 1 or len(nonces) != 1:
            raise ValueError('No unique matching transfer and authorization nonce')
        return {'transaction': tx.lower(), 'log_index': transfers[0],
                'block_hash': receipt['blockHash'].lower(), 'block_number': int(receipt['blockNumber'], 16)}
