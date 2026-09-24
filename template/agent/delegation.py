"""Agent-ID OBO for explicitly approved downstream operations."""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid

import httpx
import msal
from requests.utils import parse_dict_header

from authentication import Caller

EXCHANGE_SCOPE = 'api://AzureADTokenExchange/.default'
GRAPH_SCOPES = ('https://graph.microsoft.com/User.Read',)
GRAPH_PROFILE_URL = 'https://graph.microsoft.com/v1.0/me?$select=id,displayName'


class DelegationError(RuntimeError):
    def __init__(self, code: str, status_code: int = 503, challenge: str | None = None):
        super().__init__(code)
        self.status_code = status_code
        self.challenge = challenge


def interaction_required(claims: str) -> DelegationError:
    try:
        if not isinstance(claims, str) or len(claims) > 6000:
            raise ValueError
        parsed = json.loads(claims)
        if not isinstance(parsed, dict) or not isinstance(parsed.get('access_token'), dict):
            raise ValueError
        encoded = base64.b64encode(json.dumps(parsed, separators=(',', ':')).encode()).decode()
        return DelegationError('OBO_INTERACTION_REQUIRED', 401,
                               f'Bearer error="insufficient_claims", claims="{encoded}"')
    except (ValueError, TypeError, UnicodeError):
        return DelegationError('OBO_INVALID_CHALLENGE')


def access_token(result, *, delegated: bool) -> str:
    if not isinstance(result, dict):
        raise DelegationError('OBO_TOKEN_UNAVAILABLE')
    if 'error' in result:
        if delegated and result.get('claims'):
            raise interaction_required(result['claims'])
        codes = result.get('error_codes')
        if delegated and isinstance(codes, list) and any(code in (65001, 65004, 650052, 90094) for code in codes):
            raise DelegationError('OBO_ADMIN_CONSENT_REQUIRED', 403)
        if delegated and result.get('error') in ('interaction_required', 'invalid_grant'):
            raise DelegationError('OBO_USER_AUTHENTICATION_REQUIRED', 401, 'Bearer error="invalid_token"')
        raise DelegationError('OBO_TOKEN_UNAVAILABLE')
    token = result.get('access_token')
    if (not isinstance(token, str) or not token or len(token) > 32768
            or result.get('token_type', '').lower() != 'bearer'):
        raise DelegationError('OBO_INVALID_TOKEN_RESPONSE')
    return token


class AgentDelegation:
    def __init__(self, tenant_id: str, blueprint_id: str, agent_id: str, blueprint_credential,
                 *, identity_http: httpx.Client | None = None, graph_http: httpx.Client | None = None):
        self.tenant_id = str(uuid.UUID(tenant_id))
        self.blueprint_id = str(uuid.UUID(blueprint_id))
        self.agent_id = str(uuid.UUID(agent_id))
        self._credential = blueprint_credential
        self._identity_http = identity_http or httpx.Client(timeout=10, follow_redirects=False)
        self._graph_http = graph_http or httpx.Client(timeout=15, follow_redirects=False)
        self._owns_identity_http = identity_http is None
        self._owns_graph_http = graph_http is None
        self._managed_identity = None
        self._blueprint = None
        self._lock = threading.Lock()

    def _application(self, client_id: str, credential):
        return msal.ConfidentialClientApplication(
            client_id, client_credential=credential,
            authority=f'https://login.microsoftonline.com/{self.tenant_id}',
            http_client=self._identity_http, token_cache=msal.TokenCache(),
            exclude_scopes=['offline_access'], instance_discovery=False,
            azure_region=False, enable_pii_log=False)

    def _assertion(self) -> str:
        with self._lock:
            if self._blueprint is None:
                self._blueprint = self._application(self.blueprint_id, self._credential)
            result = self._blueprint.acquire_token_for_client(
                scopes=[EXCHANGE_SCOPE], fmi_path=self.agent_id)
            return access_token(result, delegated=False)

    def get_token(self, caller: Caller, operation: str = 'graph_me') -> str:
        if operation != 'graph_me':
            raise DelegationError('DOWNSTREAM_OPERATION_NOT_ALLOWED', 403)
        if (caller.kind != 'user' or not caller.user_assertion or caller.tenant_id != self.tenant_id):
            raise DelegationError('OBO_USER_REQUIRED', 403)
        if caller.expires_at <= time.time():
            raise DelegationError('OBO_USER_AUTHENTICATION_REQUIRED', 401, 'Bearer error="invalid_token"')
        try:
            child = self._application(self.agent_id, {'client_assertion': self._assertion})
            result = child.acquire_token_on_behalf_of(caller.user_assertion, scopes=list(GRAPH_SCOPES))
            return access_token(result, delegated=True)
        except DelegationError:
            raise
        except Exception:
            raise DelegationError('OBO_TOKEN_UNAVAILABLE') from None

    def read_profile(self, caller: Caller) -> dict:
        token = self.get_token(caller)
        try:
            response = self._graph_http.get(
                GRAPH_PROFILE_URL, headers={'Authorization': f'Bearer {token}'},
                timeout=15, follow_redirects=False)
            if response.status_code == 401:
                header = response.headers.get('WWW-Authenticate', '')
                scheme, _, parameters = header.partition(' ')
                if scheme.lower() == 'bearer' and len(parameters) <= 8192:
                    challenge = parse_dict_header(parameters)
                    if challenge.get('error') == 'insufficient_claims' and challenge.get('claims'):
                        try:
                            claims = base64.b64decode(challenge['claims'], validate=True).decode('utf-8')
                        except (ValueError, UnicodeError):
                            raise DelegationError('OBO_INVALID_CHALLENGE') from None
                        raise interaction_required(claims)
                raise DelegationError('OBO_USER_AUTHENTICATION_REQUIRED', 401, 'Bearer error="invalid_token"')
            if response.status_code == 403:
                raise DelegationError('DOWNSTREAM_ACCESS_DENIED', 403)
            if response.status_code != 200 or len(response.content) > 65536:
                raise DelegationError('DOWNSTREAM_UNAVAILABLE')
            profile = response.json()
            if (not isinstance(profile, dict) or str(uuid.UUID(profile.get('id', ''))) != caller.object_id
                    or not isinstance(profile.get('displayName'), (str, type(None)))):
                raise DelegationError('DOWNSTREAM_INVALID_RESPONSE')
            return {'id': profile['id'], 'displayName': profile.get('displayName')}
        except DelegationError:
            raise
        except Exception:
            raise DelegationError('DOWNSTREAM_UNAVAILABLE') from None

    def close(self) -> None:
        if self._owns_identity_http:
            self._identity_http.close()
        if self._owns_graph_http:
            self._graph_http.close()
        if self._managed_identity is not None:
            self._managed_identity.close()


def configure(manifest: dict) -> AgentDelegation | None:
    mode = manifest.get('authMode', 's2s')
    if mode == 's2s':
        return None
    if mode != 'obo':
        raise ValueError('Unsupported agent authMode.')
    managed_identity = None
    try:
        credential_mode = os.getenv('A365_BLUEPRINT_AUTH_MODE', 'secret')
        if credential_mode == 'managed_identity':
            from azure.identity import ManagedIdentityCredential
            managed_identity = ManagedIdentityCredential(client_id=str(uuid.UUID(os.environ['AZURE_CLIENT_ID'])))
            credential = {'client_assertion': lambda: managed_identity.get_token(EXCHANGE_SCOPE).token}
        elif credential_mode == 'secret':
            credential = os.environ['A365_BLUEPRINT_CLIENT_SECRET']
            if not credential:
                raise ValueError
        else:
            raise ValueError
        provider = AgentDelegation(os.environ['A365_TENANT_ID'], os.environ['A365_BLUEPRINT_CLIENT_ID'],
                                   os.environ['A365_AGENT_INSTANCE_ID'], credential)
        provider._managed_identity = managed_identity
        return provider
    except (KeyError, ValueError, TypeError, AttributeError):
        if managed_identity is not None:
            managed_identity.close()
        raise ValueError('OBO requires valid tenant, blueprint, agent and blueprint credential configuration.') from None