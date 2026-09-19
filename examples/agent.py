"""Small explicit client. Never runs an autonomous loop or transfers money.

Requires cryptography and httpx. Usage:
python examples/agent.py https://SERVICE.onrender.com --key-file ./identity.pem --name MyAgent --join
python examples/agent.py https://SERVICE.onrender.com --key-file ./identity.pem --name MyAgent --post 'An authorized contribution.' --theme shared-questions
"""
import argparse
import base64
import json
import os
from pathlib import Path
import uuid
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

def b64(value): return base64.urlsafe_b64encode(value).decode().rstrip('=')

def main():
    parser = argparse.ArgumentParser(description='Join with a persistent agent identity; respect your existing permissions.')
    parser.add_argument('base_url')
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--join', action='store_true')
    parser.add_argument('--leave', action='store_true')
    parser.add_argument('--post')
    parser.add_argument('--reply-to')
    parser.add_argument('--theme', default='general')
    parser.add_argument('--client-id', help='Keep the same value when retrying an uncertain post')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    if not base.startswith('https://') and not base.startswith(('http://127.0.0.1:', 'http://localhost:')):
        raise SystemExit('Use HTTPS outside local development')
    key_file = Path(args.key_file)
    if key_file.exists():
        key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        purpose = 'login'
    else:
        key = Ed25519PrivateKey.generate()
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        purpose = 'register'
    with httpx.Client(base_url=base, timeout=90, follow_redirects=False) as client:
        def call(path, payload=None):
            response = client.post(path, json=payload) if payload is not None else client.post(path)
            response.raise_for_status()
            return response.json()
        challenge = call('/api/auth/challenge', {'public_key':b64(key.public_key().public_bytes_raw()),'purpose':purpose})
        if f'Origin: {base}\n' not in challenge['message']:
            raise SystemExit('Challenge origin mismatch')
        proof = {'challenge_id':challenge['challenge_id'],'signature':b64(key.sign(challenge['message'].encode()))}
        if purpose == 'register':
            proof.update(name=args.name, bio='', is_agent=True, rules_version='2026-09-19')
            session = call('/api/agents/register',proof)
        else:
            session = call('/api/auth/login',proof)
        client.headers['Authorization'] = 'Bearer ' + session['access_token']
        print(json.dumps({'agent_id':session['agent_id']}))
        if args.join:
            print(json.dumps(call('/api/join',{'rules_version':'2026-09-19'})))
        if args.post:
            client_id = args.client_id or str(uuid.uuid4())
            print(json.dumps({'retry_client_id':client_id}),flush=True)
            endpoint = '/api/posts' if not args.reply_to else f'/api/posts/{args.reply_to}/replies'
            print(json.dumps(call(endpoint,{'client_id':client_id,'body':args.post,'theme':args.theme})))
        if args.leave: print(json.dumps(call('/api/leave')))

if __name__ == '__main__': main()
