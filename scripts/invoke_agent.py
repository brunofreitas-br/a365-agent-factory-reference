#!/usr/bin/env python3
"""Invoke an authenticated agent directly, without the Factory demo console."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from urllib.parse import urlsplit

import httpx
import msal
from requests.utils import parse_dict_header


def endpoint_url(value: str) -> str:
    parsed = urlsplit(value)
    local_http = parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme != 'https' and not local_http)):
        raise ValueError('Endpoint requires HTTPS, or HTTP on loopback only, without credentials or query.')
    return value


def claims_challenge(response: httpx.Response) -> str | None:
    header = response.headers.get('WWW-Authenticate', '')
    scheme, _, parameters = header.partition(' ')
    if response.status_code != 401 or scheme.lower() != 'bearer' or len(parameters) > 8192:
        return None
    values = parse_dict_header(parameters)
    if values.get('error') != 'insufficient_claims' or not values.get('claims'):
        return None
    try:
        claims = json.loads(base64.b64decode(values['claims'], validate=True).decode('utf-8'))
        if not isinstance(claims, dict) or not isinstance(claims.get('access_token'), dict):
            return None
        return json.dumps(claims)
    except (ValueError, UnicodeError):
        return None


def invoke(client: httpx.Client, endpoint: str, application, scope: str, prompt: str, *, user: bool):
    endpoint = endpoint_url(endpoint)
    challenge = None
    for attempt in range(2):
        if user:
            result = application.acquire_token_interactive(scopes=[scope], claims_challenge=challenge, timeout=180)
        else:
            result = application.acquire_token_for_client(scopes=[scope])
        if not isinstance(result, dict) or result.get('error') or not result.get('access_token'):
            raise RuntimeError('AUTHENTICATION_FAILED')
        response = client.post(endpoint, json={'input': prompt},
                               headers={'Authorization': 'Bearer ' + result['access_token']},
                               timeout=120, follow_redirects=False)
        challenge = claims_challenge(response) if user and attempt == 0 else None
        if challenge is None:
            return response
    raise RuntimeError('API_REQUEST_FAILED')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True, type=endpoint_url)
    parser.add_argument('--tenant-id', required=True, type=uuid.UUID)
    parser.add_argument('--blueprint-id', required=True, type=uuid.UUID)
    parser.add_argument('--client-id', required=True, type=uuid.UUID)
    parser.add_argument('--mode', choices=('user', 'application'), default='user')
    parser.add_argument('--input', required=True)
    args = parser.parse_args()
    configuration = {
        'authority': f'https://login.microsoftonline.com/{args.tenant_id}',
        'exclude_scopes': ['offline_access'], 'client_capabilities': ['CP1'],
        'instance_discovery': False, 'enable_pii_log': False,
    }
    if args.mode == 'user':
        application = msal.PublicClientApplication(str(args.client_id), **configuration)
        scope = f'api://{args.blueprint_id}/Agent.Invoke'
    else:
        secret = os.environ.get('AGENT_CLIENT_SECRET')
        if not secret:
            raise RuntimeError('APPLICATION_CREDENTIAL_REQUIRED')
        application = msal.ConfidentialClientApplication(str(args.client_id), client_credential=secret,
                                                       **configuration)
        scope = f'api://{args.blueprint_id}/.default'
    with httpx.Client() as client:
        response = invoke(client, args.endpoint, application, scope, args.input, user=args.mode == 'user')
    if response.status_code == 200:
        print(json.dumps(response.json(), ensure_ascii=False))
        return 0
    print(json.dumps({'httpStatus': response.status_code, 'error': 'AGENT_REQUEST_REJECTED'}))
    return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({'error': 'AUTHENTICATION_OR_INVOCATION_FAILED'}), file=sys.stderr)
        sys.exit(1)