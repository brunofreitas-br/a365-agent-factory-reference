import base64
import importlib.util
import json
import os
import pathlib
import sys
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from test_authentication import authentication, BLUEPRINT, CLIENT, OTHER, TENANT, USER

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / 'template/agent/delegation.py'
SPEC = importlib.util.spec_from_file_location('agent_delegation', MODULE_PATH)
delegation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = delegation
with patch.dict(sys.modules, {'authentication': authentication}):
    SPEC.loader.exec_module(delegation)
AGENT = '00000000-0000-4000-8000-000000000007'


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.graph_requests = []
        self.fmi_error = None
        self.obo_error = None
        self.graph_status = 200
        self.graph_headers = {}
        self.profile = {'id': USER, 'displayName': 'Synthetic User', 'ignored': 'not-returned'}
        self.caller = authentication.Caller(TENANT, USER, CLIENT, 'user',
                                            int(time.time()) + 600, 'synthetic-user-assertion')
        authority = f'https://login.microsoftonline.com/{TENANT}'

        def identity_request(request):
            self.assertEqual(request.url.host, 'login.microsoftonline.com')
            if request.method == 'GET':
                self.assertEqual(request.url.path, f'/{TENANT}/v2.0/.well-known/openid-configuration')
                return httpx.Response(200, json={
                    'authorization_endpoint': f'{authority}/oauth2/v2.0/authorize',
                    'token_endpoint': f'{authority}/oauth2/v2.0/token',
                    'issuer': f'{authority}/v2.0',
                })
            self.assertEqual(request.method, 'POST')
            self.assertEqual(request.url.path, f'/{TENANT}/oauth2/v2.0/token')
            body = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            self.requests.append(body)
            if body['client_id'] == BLUEPRINT:
                result = self.fmi_error or {'access_token': 'synthetic-fmi', 'expires_in': 3600,
                                            'token_type': 'Bearer'}
            else:
                self.assertEqual(body['client_id'], AGENT)
                result = self.obo_error or {'access_token': 'synthetic-downstream', 'expires_in': 3600,
                                            'token_type': 'Bearer'}
            return httpx.Response(400 if 'error' in result else 200, json=result)

        def graph_request(request):
            self.graph_requests.append(request)
            self.assertEqual(str(request.url), delegation.GRAPH_PROFILE_URL)
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.headers['Authorization'], 'Bearer synthetic-downstream')
            return httpx.Response(self.graph_status, json=self.profile, headers=self.graph_headers)

        identity_http = httpx.Client(transport=httpx.MockTransport(identity_request))
        graph_http = httpx.Client(transport=httpx.MockTransport(graph_request))
        self.addCleanup(identity_http.close)
        self.addCleanup(graph_http.close)
        self.provider = delegation.AgentDelegation(TENANT, BLUEPRINT, AGENT, 'synthetic-secret',
                                                   identity_http=identity_http, graph_http=graph_http)

    def test_msal_performs_blueprint_fmi_then_child_human_obo(self):
        profile = self.provider.read_profile(self.caller)
        self.assertEqual(profile, {'id': USER, 'displayName': 'Synthetic User'})
        self.assertEqual(len(self.requests), 2)
        first, second = self.requests
        self.assertEqual(first['client_id'], BLUEPRINT)
        self.assertEqual(first['scope'], delegation.EXCHANGE_SCOPE)
        self.assertEqual(first['grant_type'], 'client_credentials')
        self.assertEqual(first['fmi_path'], AGENT)
        self.assertEqual(second['client_id'], AGENT)
        self.assertEqual(second['client_assertion'], 'synthetic-fmi')
        self.assertEqual(second['assertion'], self.caller.user_assertion)
        self.assertEqual(second['grant_type'], 'urn:ietf:params:oauth:grant-type:jwt-bearer')
        self.assertEqual(second['requested_token_use'], 'on_behalf_of')
        self.assertIn('https://graph.microsoft.com/User.Read', second['scope'].split())
        self.assertNotIn('offline_access', second['scope'].split())
        self.assertNotIn('client_secret', second)

    def test_users_do_not_share_delegated_token_cache(self):
        self.provider.get_token(self.caller)
        other = replace(self.caller, object_id=OTHER, user_assertion='different-user-assertion')
        self.provider.get_token(other)
        self.provider.get_token(self.caller)
        fmi = [body for body in self.requests if body['client_id'] == BLUEPRINT]
        obo = [body for body in self.requests if body['client_id'] == AGENT]
        self.assertEqual(len(fmi), 1)
        self.assertEqual([body['assertion'] for body in obo],
                         [self.caller.user_assertion, other.user_assertion, self.caller.user_assertion])

    def test_app_caller_foreign_tenant_and_expired_user_never_exchange(self):
        callers = (replace(self.caller, kind='application', user_assertion=None),
                   replace(self.caller, tenant_id=OTHER), replace(self.caller, expires_at=0))
        for caller in callers:
            with self.subTest(kind=caller.kind):
                with self.assertRaises(delegation.DelegationError):
                    self.provider.read_profile(caller)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.graph_requests, [])

    def test_unapproved_operation_cannot_choose_resource_or_scope(self):
        with self.assertRaisesRegex(delegation.DelegationError, 'OPERATION_NOT_ALLOWED'):
            self.provider.get_token(self.caller, 'https://untrusted.invalid/.default')
        self.assertEqual(self.requests, [])

    def test_failed_fmi_stops_before_obo_and_graph(self):
        self.fmi_error = {'error': 'invalid_client', 'error_description': 'private-secret'}
        with self.assertRaises(delegation.DelegationError) as caught:
            self.provider.read_profile(self.caller)
        self.assertNotIn('private-secret', str(caught.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.graph_requests, [])

    def test_missing_consent_never_falls_back_to_app_only(self):
        self.obo_error = {'error': 'invalid_grant', 'error_codes': [65001], 'error_description': 'private-detail'}
        with self.assertRaises(delegation.DelegationError) as caught:
            self.provider.read_profile(self.caller)
        self.assertEqual(str(caught.exception), 'OBO_ADMIN_CONSENT_REQUIRED')
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.graph_requests, [])

    def test_identity_claims_challenge_is_sanitized_and_returned(self):
        claims = {'access_token': {'acrs': {'essential': True, 'value': 'c1'}}}
        self.obo_error = {'error': 'interaction_required', 'claims': json.dumps(claims),
                          'error_description': 'private-detail'}
        with self.assertRaises(delegation.DelegationError) as caught:
            self.provider.read_profile(self.caller)
        self.assertEqual(caught.exception.status_code, 401)
        parameters = delegation.parse_dict_header(caught.exception.challenge.removeprefix('Bearer '))
        self.assertEqual(parameters['error'], 'insufficient_claims')
        self.assertEqual(json.loads(base64.b64decode(parameters['claims'])), claims)
        self.assertNotIn('private-detail', repr(caught.exception))
        self.assertEqual(self.graph_requests, [])

    def test_graph_claims_challenge_does_not_retry_or_expose_response(self):
        self.graph_status = 401
        claims = {'access_token': {'acrs': {'value': 'c1'}}}
        encoded = base64.b64encode(json.dumps(claims).encode()).decode()
        self.graph_headers = {'WWW-Authenticate': f'Bearer error="insufficient_claims", claims="{encoded}", authorization_uri="https://untrusted.invalid"'}
        with self.assertRaises(delegation.DelegationError) as caught:
            self.provider.read_profile(self.caller)
        self.assertEqual(caught.exception.status_code, 401)
        self.assertNotIn('untrusted', caught.exception.challenge)
        self.assertEqual(len(self.graph_requests), 1)

    def test_graph_denials_redirects_and_errors_fail_closed(self):
        for status in (401, 403, 302, 429, 500):
            with self.subTest(status=status):
                self.graph_status = status
                with self.assertRaises(delegation.DelegationError):
                    self.provider.read_profile(self.caller)

    def test_profile_of_another_user_is_rejected(self):
        self.profile['id'] = OTHER
        with self.assertRaisesRegex(delegation.DelegationError, 'INVALID_RESPONSE'):
            self.provider.read_profile(self.caller)

    def test_malformed_challenge_is_not_relayed(self):
        for claims in ('not-json', '[]', '{}', 'a' * 6001):
            error = delegation.interaction_required(claims)
            self.assertEqual(error.status_code, 503)
            self.assertIsNone(error.challenge)

    def test_missing_or_invalid_token_is_not_sent_to_graph(self):
        for response in ({}, {'access_token': '', 'token_type': 'Bearer'},
                         {'access_token': 'secret', 'token_type': 'unsupported'}):
            with self.subTest(response=response):
                with self.assertRaises(delegation.DelegationError):
                    delegation.access_token(response, delegated=True)


class DelegationConfigurationTests(unittest.TestCase):
    def test_s2s_does_not_require_or_initialize_obo(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(delegation.msal, 'ConfidentialClientApplication') as factory:
            self.assertIsNone(delegation.configure({'authMode': 's2s'}))
            factory.assert_not_called()

    def test_obo_configuration_is_explicit_and_does_not_connect_at_startup(self):
        environment = {'A365_TENANT_ID': TENANT, 'A365_BLUEPRINT_CLIENT_ID': BLUEPRINT,
                       'A365_AGENT_INSTANCE_ID': AGENT, 'A365_BLUEPRINT_CLIENT_SECRET': 'synthetic-secret'}
        with patch.dict(os.environ, environment, clear=True), patch.object(delegation.msal, 'ConfidentialClientApplication') as factory:
            provider = delegation.configure({'authMode': 'obo'})
            self.addCleanup(provider.close)
            self.assertEqual(provider.agent_id, AGENT)
            factory.assert_not_called()
        for field in environment:
            with self.subTest(field=field), patch.dict(os.environ, {key: value for key, value in environment.items() if key != field}, clear=True):
                with self.assertRaises(ValueError):
                    delegation.configure({'authMode': 'obo'})


if __name__ == '__main__':
    unittest.main()