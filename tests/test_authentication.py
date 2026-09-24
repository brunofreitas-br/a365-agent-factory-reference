import importlib.util
import base64
import json
import os
import pathlib
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jwt
import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / 'template/agent/authentication.py'
SPEC = importlib.util.spec_from_file_location('agent_authentication', MODULE_PATH)
authentication = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = authentication
SPEC.loader.exec_module(authentication)

TENANT = '00000000-0000-4000-8000-000000000001'
BLUEPRINT = '00000000-0000-4000-8000-000000000002'
CLIENT = '00000000-0000-4000-8000-000000000003'
USER = '00000000-0000-4000-8000-000000000004'
OTHER = '00000000-0000-4000-8000-000000000005'


class EntraAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.keys = Mock()
        self.keys.get_signing_key_from_jwt.return_value = SimpleNamespace(key=self.private_key.public_key())
        self.auth = authentication.EntraAuthenticator(TENANT, BLUEPRINT, [CLIENT], key_client=self.keys)
        now = int(time.time())
        self.claims = {
            'aud': BLUEPRINT, 'iss': f'https://login.microsoftonline.com/{TENANT}/v2.0',
            'tid': TENANT, 'ver': '2.0', 'azp': CLIENT, 'oid': USER, 'sub': 'synthetic-user',
            'exp': now + 600, 'nbf': now - 10, 'iat': now - 10, 'scp': 'Agent.Invoke',
        }

    def token(self, *, claims=None, headers=None, key=None):
        return jwt.encode(claims if claims is not None else self.claims,
                          key or self.private_key, algorithm='RS256',
                          headers={'kid': 'synthetic-key', **(headers or {})})

    def test_user_token_preserves_assertion_outside_representation(self):
        token = self.token()
        caller = self.auth.authenticate_token(token)
        self.assertEqual(caller.kind, 'user')
        self.assertEqual(caller.object_id, USER)
        self.assertEqual(caller.client_id, CLIENT)
        self.assertEqual(caller.user_assertion, token)
        self.assertNotIn(token, repr(caller))

    def test_application_token_requires_role_and_never_becomes_user_assertion(self):
        self.claims.pop('scp')
        self.claims.update(idtyp='app', roles=['Agent.Invoke.Application'])
        caller = self.auth.authenticate_token(self.token())
        self.assertEqual(caller.kind, 'application')
        self.assertIsNone(caller.user_assertion)

    def test_rejects_foreign_tenant_audience_issuer_and_version(self):
        for field, value in (('tid', OTHER), ('aud', OTHER), ('aud', [BLUEPRINT]),
                             ('iss', f'https://login.microsoftonline.com/{OTHER}/v2.0'),
                             ('iss', f'https://sts.windows.net/{TENANT}/'), ('ver', '1.0')):
            with self.subTest(field=field):
                with self.assertRaises(authentication.InvalidAuthentication):
                    self.auth.authenticate_token(self.token(claims={**self.claims, field: value}))

    def test_rejects_bad_signature(self):
        with self.assertRaises(authentication.InvalidAuthentication):
            self.auth.authenticate_token(self.token(key=self.other_key))

    def test_rejects_expired_future_and_malformed_timestamps(self):
        now = int(time.time())
        cases = ({'exp': now - 60}, {'nbf': now + 300}, {'iat': now + 300},
                 {'exp': str(now + 300)}, {'iat': True}, {'exp': self.claims['nbf']})
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(authentication.InvalidAuthentication):
                    self.auth.authenticate_token(self.token(claims={**self.claims, **overrides}))

    def test_every_required_claim_is_enforced(self):
        for field in ('aud', 'iss', 'tid', 'exp', 'nbf', 'iat', 'oid', 'sub', 'azp', 'ver'):
            with self.subTest(field=field):
                claims = {name: value for name, value in self.claims.items() if name != field}
                with self.assertRaises(authentication.InvalidAuthentication):
                    self.auth.authenticate_token(self.token(claims=claims))

    def test_unknown_client_cannot_use_an_otherwise_valid_token(self):
        with self.assertRaises(authentication.ForbiddenCaller):
            self.auth.authenticate_token(self.token(claims={**self.claims, 'azp': OTHER}))

    def test_scope_requires_exact_whitespace_separated_permission(self):
        for scopes in ('User.Read', 'prefixAgent.Invoke', '', ['Agent.Invoke']):
            with self.subTest(scopes=scopes):
                with self.assertRaises(authentication.ForbiddenCaller):
                    self.auth.authenticate_token(self.token(claims={**self.claims, 'scp': scopes}))
        caller = self.auth.authenticate_token(self.token(claims={**self.claims, 'scp': 'other Agent.Invoke'}))
        self.assertEqual(caller.kind, 'user')

    def test_id_token_or_ambiguous_principal_is_not_accepted(self):
        claims = {name: value for name, value in self.claims.items() if name != 'scp'}
        for extra in ({}, {'roles': ['Agent.Invoke']}, {'idtyp': 'user', 'roles': ['Agent.Invoke']},
                      {'idtyp': 'app', 'scp': 'Agent.Invoke'}):
            with self.subTest(extra=extra):
                with self.assertRaises(authentication.InvalidAuthentication):
                    self.auth.authenticate_token(self.token(claims={**claims, **extra}))

    def test_app_role_is_required_and_must_be_a_list(self):
        claims = {name: value for name, value in self.claims.items() if name != 'scp'}
        for roles in (None, [], ['Reader'], ['Agent.Invoke'], 'Agent.Invoke.Application', [1, 'Agent.Invoke.Application']):
            with self.subTest(roles=roles):
                with self.assertRaises(authentication.ForbiddenCaller):
                    self.auth.authenticate_token(self.token(claims={**claims, 'idtyp': 'app', 'roles': roles}))

    def test_rejects_untrusted_key_sources_and_critical_headers_before_jwks(self):
        for header in ({'jku': 'https://untrusted.invalid/keys'}, {'x5u': 'https://untrusted.invalid/cert'},
                       {'jwk': {}}, {'crit': []}, {'kid': ''}, {'kid': 'a' * 129}):
            with self.subTest(header=header):
                with self.assertRaises(authentication.InvalidAuthentication):
                    self.auth.authenticate_token(self.token(headers=header))
        self.keys.get_signing_key_from_jwt.assert_not_called()

    def test_rejects_none_and_symmetric_algorithms(self):
        tokens = (jwt.encode(self.claims, None, algorithm='none', headers={'kid': 'synthetic-key'}),
                  jwt.encode(self.claims, 'synthetic-key', algorithm='HS256', headers={'kid': 'synthetic-key'}))
        for token in tokens:
            with self.subTest(token_type=jwt.get_unverified_header(token)['alg']):
                with self.assertRaises(authentication.InvalidAuthentication):
                    self.auth.authenticate_token(token)
        self.keys.get_signing_key_from_jwt.assert_not_called()

    def test_unavailable_keys_fail_closed_without_leaking_errors(self):
        self.keys.get_signing_key_from_jwt.side_effect = PyJWKClientConnectionError('private-detail')
        with self.assertRaises(authentication.AuthenticationUnavailable) as caught:
            self.auth.authenticate_token(self.token())
        self.assertNotIn('private-detail', str(caught.exception))

    def test_unknown_signing_key_is_invalid_not_authorized(self):
        self.keys.get_signing_key_from_jwt.side_effect = PyJWKClientError('private-detail')
        with self.assertRaises(authentication.InvalidAuthentication) as caught:
            self.auth.authenticate_token(self.token())
        self.assertNotIn('private-detail', str(caught.exception))

    def test_invalid_identity_claims_and_oversize_token_do_not_pass(self):
        for field, value in (('oid', 'not-a-guid'), ('azp', ''), ('sub', '')):
            with self.subTest(field=field):
                with self.assertRaises(authentication.InvalidAuthentication):
                    self.auth.authenticate_token(self.token(claims={**self.claims, field: value}))
        for token in ('', 'malformed-token', 'a' * 16385):
            with self.assertRaises(authentication.InvalidAuthentication):
                self.auth.authenticate_token(token)


class AuthenticationConfigurationTests(unittest.TestCase):
    def test_configuration_requires_explicit_allowlist(self):
        for clients in ([], {}, 'client', [None], ['not-a-guid']):
            with self.subTest(clients=clients):
                with patch.dict(os.environ, {'A365_TENANT_ID': TENANT,
                                            'A365_BLUEPRINT_CLIENT_ID': BLUEPRINT,
                                            'API_ALLOWED_CLIENT_IDS': json.dumps(clients)}, clear=True):
                    with self.assertRaises(ValueError):
                        authentication.configure()

    def test_no_implicit_anonymous_or_external_provider_mode(self):
        for provider in ('none', 'disabled', 'external'):
            with patch.dict(os.environ, {'API_AUTH_PROVIDER': provider}, clear=True):
                with self.assertRaises(ValueError):
                    authentication.configure()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                authentication.configure()

    def test_authority_and_keys_are_derived_only_from_server_configuration(self):
        with patch.dict(os.environ, {'A365_TENANT_ID': TENANT, 'A365_BLUEPRINT_CLIENT_ID': BLUEPRINT,
                                    'API_ALLOWED_CLIENT_IDS': json.dumps([CLIENT])}, clear=True):
            validator = authentication.configure()
        self.assertEqual(validator.audience, BLUEPRINT)
        self.assertEqual(validator._keys.uri, f'https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys')


class FactoryApiConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = MODULE_PATH.parents[2] / 'scripts/provision_identity.py'
        spec = importlib.util.spec_from_file_location('provision_api_test', path)
        cls.provision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.provision)

    def test_api_permissions_are_stable_and_preserve_existing_definitions(self):
        blueprint = {'appId': BLUEPRINT, 'api': {'oauth2PermissionScopes': [
            {'id': OTHER, 'value': 'Existing.Read', 'isEnabled': True}]},
            'appRoles': [{'id': CLIENT, 'value': 'Existing.Role', 'isEnabled': True}],
            'optionalClaims': {'idToken': [{'name': 'email'}]},
            'identifierUris': ['api://existing']}
        before = json.dumps(blueprint, sort_keys=True)
        configured = self.provision.api_configuration(blueprint)
        self.assertEqual(json.dumps(blueprint, sort_keys=True), before)
        self.assertEqual(configured['api']['requestedAccessTokenVersion'], 2)
        self.assertEqual(configured['api']['oauth2PermissionScopes'][0]['value'], 'Existing.Read')
        self.assertEqual(configured['appRoles'][0]['value'], 'Existing.Role')
        self.assertEqual(configured['optionalClaims']['idToken'], [{'name': 'email'}])
        self.assertIn('api://existing', configured['identifierUris'])
        self.assertIn(f'api://{BLUEPRINT}', configured['identifierUris'])
        self.assertEqual(self.provision.api_configuration({'appId': BLUEPRINT, **configured}), configured)

    def test_existing_incompatible_permissions_require_review(self):
        candidates = (
            {'api': {'oauth2PermissionScopes': [{'value': 'Agent.Invoke', 'type': 'User', 'isEnabled': True}]}},
            {'appRoles': [{'value': 'Agent.Invoke.Application', 'isEnabled': False, 'allowedMemberTypes': ['Application']}]},
        )
        for override in candidates:
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    self.provision.api_configuration({'appId': BLUEPRINT, **override})

    def test_configuring_api_does_not_grant_permissions(self):
        blueprint = {'id': OTHER, 'appId': BLUEPRINT}
        graph = Mock(dry_run=False)
        graph.call.side_effect = [(200, blueprint), (204, {})]
        self.provision.ensure_api(graph, blueprint)
        self.assertEqual([call.args[0] for call in graph.call.call_args_list], ['GET', 'PATCH'])
        self.assertEqual(graph.call.call_args.args[1],
                         f'https://graph.microsoft.com/beta/applications/{OTHER}/microsoft.graph.agentIdentityBlueprint')
        self.assertNotIn('requiredResourceAccess', graph.call.call_args.args[2])

    def test_delegated_and_application_permissions_have_distinct_values(self):
        configuration = self.provision.api_configuration({'appId': BLUEPRINT})
        scopes = {item['value'] for item in configuration['api']['oauth2PermissionScopes']}
        roles = {item['value'] for item in configuration['appRoles']}
        self.assertEqual(scopes, {'Agent.Invoke'})
        self.assertEqual(roles, {'Agent.Invoke.Application'})
        self.assertFalse(scopes & roles)

    def test_existing_api_contract_is_not_rewritten(self):
        blueprint = {'id': OTHER, 'appId': BLUEPRINT}
        current = {**blueprint, **self.provision.api_configuration(blueprint)}
        graph = Mock(dry_run=False)
        graph.call.return_value = (200, current)
        self.provision.ensure_api(graph, blueprint)
        self.assertEqual(graph.call.call_count, 1)

    def test_only_changed_api_properties_are_patched(self):
        blueprint = {'id': OTHER, 'appId': BLUEPRINT}
        current = {**blueprint, **self.provision.api_configuration(blueprint)}
        current['api']['requestedAccessTokenVersion'] = None
        graph = Mock(dry_run=False)
        graph.call.side_effect = [(200, current), (204, {})]
        self.provision.ensure_api(graph, blueprint)
        self.assertEqual(set(graph.call.call_args.args[2]), {'api'})


class FactoryRequestPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = MODULE_PATH.parents[2] / 'scripts/validate_request.py'
        spec = importlib.util.spec_from_file_location('validate_api_request_test', path)
        cls.validation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.validation)

    def test_obo_does_not_silently_enable_workiq_or_autonomous_execution(self):
        for request in ({'auth_mode': 'obo', 'workiq_tools': ['mail']},
                        {'auth_mode': 's2s', 'workiq_tools': ['mail']},
                        {'auth_mode': 'obo', 'autonomous': True}):
            with self.subTest(request=request):
                self.assertTrue(any(finding.level == 'erro' for finding in self.validation.check_policy(request)))
        self.assertFalse(any(finding.level == 'erro' for finding in self.validation.check_policy(
            {'auth_mode': 'obo', 'workiq_tools': [], 'autonomous': False})))


class IndependentClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = MODULE_PATH.parents[2] / 'scripts/invoke_agent.py'
        spec = importlib.util.spec_from_file_location('independent_client_test', path)
        cls.consumer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.consumer)

    def test_token_is_only_sent_to_explicit_https_endpoint_in_header(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={'output': 'synthetic'})

        application = Mock()
        application.acquire_token_for_client.return_value = {'access_token': 'synthetic-token'}
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            response = self.consumer.invoke(client, 'https://agent.invalid/invoke', application,
                                            f'api://{BLUEPRINT}/.default', 'synthetic', user=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers['Authorization'], 'Bearer synthetic-token')
        self.assertEqual(json.loads(requests[0].content), {'input': 'synthetic'})
        application.acquire_token_interactive.assert_not_called()

    def test_redirect_is_not_followed_with_credentials(self):
        application = Mock()
        application.acquire_token_for_client.return_value = {'access_token': 'synthetic-token'}
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(302, headers={'Location': 'https://untrusted.invalid'})

        with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
            response = self.consumer.invoke(client, 'https://agent.invalid/invoke', application,
                                            f'api://{BLUEPRINT}/.default', 'synthetic', user=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(requests), 1)

    def test_user_claims_challenge_reauthenticates_once_without_s2s(self):
        application = Mock()
        application.acquire_token_interactive.return_value = {'access_token': 'synthetic-token'}
        claims = {'access_token': {'acrs': {'value': 'c1'}}}
        encoded = base64.b64encode(json.dumps(claims).encode()).decode()
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(401, headers={'WWW-Authenticate': f'Bearer error="insufficient_claims", claims="{encoded}"'})

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            response = self.consumer.invoke(client, 'https://agent.invalid/invoke', application,
                                            f'api://{BLUEPRINT}/Agent.Invoke', 'synthetic', user=True)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(requests), 2)
        self.assertEqual(json.loads(application.acquire_token_interactive.call_args.kwargs['claims_challenge']), claims)
        application.acquire_token_for_client.assert_not_called()

    def test_non_tls_remote_or_token_in_url_is_rejected(self):
        for endpoint in ('http://agent.invalid/invoke', 'https://user:secret@agent.invalid/invoke',
                         'https://agent.invalid/invoke?access_token=secret', 'https://agent.invalid/#secret'):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    self.consumer.endpoint_url(endpoint)
        self.assertEqual(self.consumer.endpoint_url('http://127.0.0.1:8000/invoke'),
                         'http://127.0.0.1:8000/invoke')


if __name__ == '__main__':
    unittest.main()