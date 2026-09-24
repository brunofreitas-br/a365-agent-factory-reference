"""Validate API callers independently of the hosting platform or demo console."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Literal

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError


class InvalidAuthentication(RuntimeError):
    pass


class ForbiddenCaller(RuntimeError):
    pass


class AuthenticationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Caller:
    tenant_id: str
    object_id: str
    client_id: str
    kind: Literal['user', 'application']
    expires_at: int
    user_assertion: str | None = field(default=None, repr=False, compare=False)


class EntraAuthenticator:
    def __init__(self, tenant_id: str, audience: str, allowed_clients: list[str], *, key_client=None):
        self.tenant_id = str(uuid.UUID(tenant_id))
        self.audience = str(uuid.UUID(audience))
        if not isinstance(allowed_clients, list) or not allowed_clients:
            raise ValueError('API_ALLOWED_CLIENT_IDS requires a nonempty JSON array of client IDs.')
        self.allowed_clients = frozenset(str(uuid.UUID(value)) for value in allowed_clients)
        self.issuer = f'https://login.microsoftonline.com/{self.tenant_id}/v2.0'
        self._keys = key_client or PyJWKClient(
            f'https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys',
            cache_jwk_set=True, lifespan=300, timeout=5)

    def authenticate_token(self, token: str) -> Caller:
        try:
            if not isinstance(token, str) or not 0 < len(token) <= 16384:
                raise InvalidAuthentication('INVALID_ACCESS_TOKEN')
            header = jwt.get_unverified_header(token)
            if (header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str)
                    or not 0 < len(header['kid']) <= 128
                    or any(name in header for name in ('crit', 'jku', 'jwk', 'x5u'))):
                raise InvalidAuthentication('INVALID_ACCESS_TOKEN')
            signing_key = self._keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, signing_key.key, algorithms=['RS256'], audience=self.audience,
                issuer=self.issuer, leeway=30, options={
                    'strict_aud': True,
                    'require': ['aud', 'iss', 'tid', 'exp', 'nbf', 'iat', 'oid', 'sub', 'azp', 'ver'],
                })
            if claims['ver'] != '2.0' or claims['tid'] != self.tenant_id:
                raise InvalidAuthentication('INVALID_ACCESS_TOKEN')
            if (any(type(claims[name]) is not int for name in ('exp', 'nbf', 'iat'))
                    or claims['exp'] <= claims['iat'] or claims['exp'] <= claims['nbf']):
                raise InvalidAuthentication('INVALID_ACCESS_TOKEN')
            object_id = str(uuid.UUID(claims['oid']))
            client_id = str(uuid.UUID(claims['azp']))
            if not isinstance(claims['sub'], str) or not claims['sub']:
                raise InvalidAuthentication('INVALID_ACCESS_TOKEN')
            if client_id not in self.allowed_clients:
                raise ForbiddenCaller('CLIENT_NOT_AUTHORIZED')
            if 'scp' in claims:
                if claims.get('idtyp') not in (None, 'user'):
                    raise InvalidAuthentication('INVALID_ACCESS_TOKEN')
                scopes = claims['scp']
                if not isinstance(scopes, str) or 'Agent.Invoke' not in scopes.split():
                    raise ForbiddenCaller('INVOCATION_PERMISSION_REQUIRED')
                return Caller(self.tenant_id, object_id, client_id, 'user', claims['exp'], token)
            roles = claims.get('roles')
            if claims.get('idtyp') != 'app':
                raise InvalidAuthentication('APPLICATION_TOKEN_REQUIRED')
            if (not isinstance(roles, list) or not all(isinstance(role, str) for role in roles)
                    or 'Agent.Invoke.Application' not in roles):
                raise ForbiddenCaller('INVOCATION_PERMISSION_REQUIRED')
            return Caller(self.tenant_id, object_id, client_id, 'application', claims['exp'])
        except (InvalidAuthentication, ForbiddenCaller):
            raise
        except PyJWKClientConnectionError:
            raise AuthenticationUnavailable('SIGNING_KEYS_UNAVAILABLE') from None
        except (jwt.InvalidTokenError, PyJWKClientError, ValueError, TypeError, KeyError, AttributeError):
            raise InvalidAuthentication('INVALID_ACCESS_TOKEN') from None


def configure() -> EntraAuthenticator:
    if os.getenv('API_AUTH_PROVIDER', 'entra') != 'entra':
        raise ValueError('Only the entra API authentication provider is implemented.')
    try:
        return EntraAuthenticator(
            os.environ['A365_TENANT_ID'], os.environ['A365_BLUEPRINT_CLIENT_ID'],
            json.loads(os.environ['API_ALLOWED_CLIENT_IDS']))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ValueError('API authentication requires tenant, blueprint audience and authorized client IDs.') from None