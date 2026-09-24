import importlib.util
import json
import os
import pathlib
import sys
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "template/agent/purview.py"
SPEC = importlib.util.spec_from_file_location("agent_purview", MODULE_PATH)
purview = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = purview
SPEC.loader.exec_module(purview)

AGENT_USER_ID = "00000000-0000-4000-8000-000000000001"
APPLICATION_ID = "00000000-0000-4000-8000-000000000002"
AGENT_ID = "00000000-0000-4000-8000-000000000003"
BLUEPRINT_ID = "00000000-0000-4000-8000-000000000004"
CONVERSATION_ID = "00000000-0000-4000-8000-000000000005"
BLOCK_ACTION = {
    "@odata.type": "#microsoft.graph.restrictAccessAction",
    "action": "restrictAccess",
    "restrictionAction": "block",
}


class PurviewPolicyTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.scopes = [{
            "activities": "uploadText,downloadText",
            "executionMode": "evaluateInline",
            "locations": [{
                "@odata.type": "#microsoft.graph.policyLocationApplication",
                "value": APPLICATION_ID,
            }],
            "policyActions": [],
        }]
        self.process_response = {
            "protectionScopeState": "notModified",
            "policyActions": [],
            "processingErrors": [],
        }
        self.process_status = 200
        self.compute_etag = '"scope-v1"'
        self.process_responses = []

        def handle(request):
            self.requests.append(request)
            if request.url.path.endswith("/protectionScopes/compute"):
                return httpx.Response(200, json={"value": self.scopes}, headers={"ETag": self.compute_etag})
            if self.process_responses:
                response = self.process_responses.pop(0)
                self.compute_etag = '"scope-v2"'
                return httpx.Response(200, json=response)
            return httpx.Response(self.process_status, json=self.process_response)

        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        self.policy = purview.PurviewPolicyClient(
            agent_user_id=AGENT_USER_ID,
            application_id=APPLICATION_ID,
            agent_id=AGENT_ID,
            blueprint_id=BLUEPRINT_ID,
            agent_name="Synthetic Agent",
            token_provider=lambda: "synthetic-graph-token",
            client=client,
        )

    def check(self, content="synthetic content", **overrides):
        arguments = {
            "activity": "uploadText",
            "checkpoint": "prompt",
            "correlation_id": CONVERSATION_ID,
            "sequence_number": 0,
        }
        arguments.update(overrides)
        return self.policy.check_text(content, **arguments)

    def test_block_stops_content_processing(self):
        self.process_response["policyActions"] = [BLOCK_ACTION]
        with self.assertRaises(purview.PurviewBlockedError):
            self.check()
        self.assertEqual(len(self.requests), 2)

    def test_policy_subject_and_agent_metadata_are_fixed(self):
        self.check()
        for request in self.requests:
            self.assertIn(f"/users/{AGENT_USER_ID}/", request.url.path)
            self.assertEqual(request.headers["Authorization"], "Bearer synthetic-graph-token")
        request = self.requests[-1]
        self.assertEqual(request.headers["If-None-Match"], '"scope-v1"')
        payload = json.loads(request.content)["contentToProcess"]
        self.assertEqual(payload["protectedAppMetadata"]["applicationLocation"]["value"], APPLICATION_ID)
        self.assertEqual(payload["contentEntries"][0]["agents"][0]["identifier"], AGENT_ID)
        self.assertEqual(payload["contentEntries"][0]["agents"][0]["blueprintId"], BLUEPRINT_ID)

    def test_api_error_never_allows_or_exposes_response(self):
        self.process_status = 403
        self.process_response = {"error": "private-content-and-token"}
        with self.assertRaises(purview.PurviewUnavailableError) as caught:
            self.check()
        self.assertNotIn("private-content-and-token", str(caught.exception))

    def test_audit_action_is_recorded_without_content(self):
        self.process_response["policyActions"] = [{**BLOCK_ACTION, "restrictionAction": "audit"}]
        with self.assertLogs(purview.log, level="INFO") as captured:
            self.check("private-content")
        logs = "\n".join(captured.output)
        self.assertIn("result=audited", logs)
        for private_value in ("private-content", AGENT_USER_ID, "synthetic-graph-token"):
            self.assertNotIn(private_value, logs)

    def test_warn_requires_confirmation_and_does_not_release(self):
        self.process_response["policyActions"] = [{**BLOCK_ACTION, "restrictionAction": "warn"}]
        with self.assertRaisesRegex(purview.PurviewBlockedError, "CONFIRMATION_REQUIRED"):
            self.check()

    def test_block_in_compute_stops_before_content_submission(self):
        self.scopes[0]["policyActions"] = [BLOCK_ACTION]
        with self.assertRaises(purview.PurviewBlockedError):
            self.check()
        self.assertEqual(len(self.requests), 1)

    def test_no_scope_or_offline_scope_is_not_inline_protection(self):
        candidates = ([], [{**self.scopes[0], "executionMode": "evaluateOffline"}],
                      [{**self.scopes[0], "activities": "downloadText"}],
                      [{**self.scopes[0], "locations": [{"value": BLUEPRINT_ID}]}])
        for scopes in candidates:
            with self.subTest(scopes=scopes):
                self.policy._cache = None
                self.scopes = scopes
                with self.assertRaisesRegex(purview.PurviewUnavailableError, "INLINE_POLICY_REQUIRED"):
                    self.check()
        self.assertTrue(all(request.url.path.endswith("/compute") for request in self.requests))

    def test_most_restrictive_matching_scope_wins(self):
        self.scopes.insert(0, {**self.scopes[0], "executionMode": "evaluateOffline"})
        self.check()
        self.assertEqual(len(self.requests), 2)

    def test_cached_scopes_are_reused(self):
        self.check()
        self.check(sequence_number=1)
        self.assertEqual(sum(request.url.path.endswith("/compute") for request in self.requests), 1)
        self.assertEqual(len(self.requests), 3)

    def test_expired_scopes_are_recomputed(self):
        with patch.object(purview.time, "monotonic", return_value=10):
            self.check()
        with patch.object(purview.time, "monotonic", return_value=311):
            self.check(sequence_number=1)
        self.assertEqual(sum(request.url.path.endswith("/compute") for request in self.requests), 2)

    def test_modified_policy_is_refreshed_before_allowing(self):
        self.process_responses = [{**self.process_response, "protectionScopeState": "modified"}]
        self.check()
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(self.requests[-1].url.path.endswith("/compute"))
        self.assertEqual(self.policy._cache[1], '"scope-v2"')

    def test_modified_state_refreshes_scopes_without_reprocessing_content(self):
        self.process_response["protectionScopeState"] = "modified"
        self.check()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(sum(request.url.path.endswith("/processContent") for request in self.requests), 1)
        self.assertIsNotNone(self.policy._cache)

    def test_modified_state_honors_refreshed_restrictions(self):
        self.process_response["protectionScopeState"] = "modified"
        original = self.scopes[0]
        candidates = (
            ([], purview.PurviewUnavailableError),
            ([{**original, "executionMode": "evaluateOffline"}], purview.PurviewUnavailableError),
            ([{**original, "policyActions": [BLOCK_ACTION]}], purview.PurviewBlockedError),
            ([{**original, "policyActions": [{**BLOCK_ACTION, "restrictionAction": "warn"}]}],
             purview.PurviewBlockedError),
        )
        for scopes, exception in candidates:
            with self.subTest(scopes=scopes):
                self.requests.clear()
                with patch.object(self.policy, "_protection_scopes", side_effect=[
                        (self.scopes, '"scope-v1"'), (scopes, '"scope-v2"')]):
                    with self.assertRaises(exception):
                        self.check()
                self.assertEqual(len(self.requests), 1)

    def test_modified_state_refresh_failure_does_not_allow(self):
        self.process_response["protectionScopeState"] = "modified"
        with patch.object(self.policy, "_protection_scopes", side_effect=[
                (self.scopes, '"scope-v1"'), purview.PurviewUnavailableError("PURVIEW_HTTP_503")]):
            with self.assertRaisesRegex(purview.PurviewUnavailableError, "HTTP_503"):
                self.check()
        self.assertEqual(len(self.requests), 1)

    def test_refreshed_audit_is_recorded_without_content(self):
        self.process_response["protectionScopeState"] = "modified"
        refreshed = [{**self.scopes[0], "policyActions": [{**BLOCK_ACTION, "restrictionAction": "audit"}]}]
        with patch.object(self.policy, "_protection_scopes", side_effect=[
                (self.scopes, '"scope-v1"'), (refreshed, '"scope-v2"')]):
            with self.assertLogs(purview.log, level="INFO") as captured:
                self.check("private-content")
        self.assertIn("result=audited", "\n".join(captured.output))
        self.assertNotIn("private-content", "\n".join(captured.output))

    def test_modified_policy_block_is_not_retried_as_allow(self):
        self.process_response.update(protectionScopeState="modified", policyActions=[BLOCK_ACTION])
        with self.assertRaises(purview.PurviewBlockedError):
            self.check()
        self.assertEqual(len(self.requests), 2)
        self.assertIsNone(self.policy._cache)

    def test_incomplete_or_unknown_decisions_do_not_allow(self):
        base = dict(self.process_response)
        for response in ({}, {**base, "processingErrors": [{"message": "private-detail"}]},
                         {**base, "policyActions": None},
                         {**base, "policyActions": [{"action": "unknownFutureValue"}]},
                         {**base, "protectionScopeState": "unknownFutureValue"}):
            with self.subTest(response=response):
                self.process_response = response
                with self.assertRaises(purview.PurviewUnavailableError):
                    self.check()

    def test_async_acceptance_and_http_errors_are_not_inline_decisions(self):
        for status_code in (202, 204, 302, 401, 403, 429, 500):
            with self.subTest(status_code=status_code):
                self.process_status = status_code
                with self.assertRaises(purview.PurviewUnavailableError):
                    self.check()

    def test_missing_token_and_network_failures_are_sanitized(self):
        for provider in (Mock(return_value=""), Mock(side_effect=RuntimeError("private-token"))):
            with self.subTest(provider=provider):
                self.policy._token_provider = provider
                with self.assertRaises(purview.PurviewUnavailableError) as caught:
                    self.check()
                self.assertNotIn("private-token", str(caught.exception))
        self.assertEqual(self.requests, [])

    def test_full_content_is_submitted_without_truncation(self):
        content = "synthetic " * 700 + " sensitive-tail"
        self.check(content)
        entry = json.loads(self.requests[-1].content)["contentToProcess"]["contentEntries"][0]
        self.assertEqual(entry["content"]["data"], content)
        self.assertFalse(entry["isTruncated"])

    def test_oversize_content_is_stopped_before_submission(self):
        with self.assertRaisesRegex(purview.PurviewUnavailableError, "CONTENT_SIZE_UNSUPPORTED"):
            self.check("x" * (purview.MAX_CONTENT_BYTES + 1))
        self.assertEqual(self.requests, [])

    def test_unknown_scope_structure_is_not_permission(self):
        original = dict(self.scopes[0])
        for scope in (None, {**original, "executionMode": "unknownFutureValue"},
                      {**original, "locations": None},
                      {**original, "locations": [{"@odata.type": 42, "value": APPLICATION_ID}]}):
            with self.subTest(scope=scope):
                self.scopes = [scope]
                self.policy._cache = None
                with self.assertRaises(purview.PurviewUnavailableError):
                    self.check()

    def test_missing_etag_is_not_cached_as_protection(self):
        self.compute_etag = ""
        with self.assertRaisesRegex(purview.PurviewUnavailableError, "INVALID_SCOPES"):
            self.check()
        self.assertIsNone(self.policy._cache)

    def test_output_requires_its_own_activity_scope(self):
        self.check(activity="downloadText", checkpoint="agent_response", sequence_number=2)
        activity = json.loads(self.requests[-1].content)["contentToProcess"]["activityMetadata"]
        self.assertEqual(activity["activity"], "downloadText")
        with self.assertRaises(ValueError):
            self.check(activity="uploadText", checkpoint="agent_response")

    def test_service_principal_cannot_be_used_as_agent_user(self):
        with self.assertRaisesRegex(ValueError, "Agent User ID"):
            purview.PurviewPolicyClient(
                agent_user_id=AGENT_ID, application_id=APPLICATION_ID, agent_id=AGENT_ID,
                blueprint_id=BLUEPRINT_ID, agent_name="Synthetic Agent", token_provider=Mock())


class ConfigurePolicyTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "PURVIEW_ENABLED": "true",
            "PURVIEW_AGENT_USER_ID": AGENT_USER_ID,
            "A365_TENANT_ID": "00000000-0000-4000-8000-000000000006",
            "A365_AGENT_INSTANCE_ID": AGENT_ID,
            "A365_BLUEPRINT_CLIENT_ID": BLUEPRINT_ID,
            "A365_BLUEPRINT_CLIENT_SECRET": "synthetic-blueprint-secret",
        }

    def test_disabled_configuration_needs_no_identity(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(purview.configure({}))

    def test_enabled_configuration_requires_every_identity_field(self):
        for name in self.environment:
            if name == "PURVIEW_ENABLED":
                continue
            with self.subTest(name=name):
                environment = {key: value for key, value in self.environment.items() if key != name}
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, name):
                        purview.configure({"displayName": "Synthetic Agent"})

    def test_flags_do_not_silently_disable_protection(self):
        for name in ("PURVIEW_ENABLED", "PURVIEW_CHECK_OUTPUT"):
            with self.subTest(name=name):
                with patch.dict(os.environ, {**self.environment, name: "invalid"}, clear=True):
                    with self.assertRaisesRegex(ValueError, name):
                        purview.configure({"displayName": "Synthetic Agent"})

    def test_enabled_client_uses_graph_resource_and_fixed_agent_user(self):
        factory = Mock()
        with patch.dict(os.environ, self.environment, clear=True):
            with patch.dict(sys.modules, {"observability": SimpleNamespace(A365TokenService=factory)}):
                policy = purview.configure({"displayName": "Synthetic Agent"})
        self.addCleanup(policy.close)
        self.assertEqual(policy.agent_user_id, AGENT_USER_ID)
        self.assertEqual(policy.application_id, AGENT_ID)
        self.assertFalse(policy.check_output)
        self.assertEqual(factory.call_args.kwargs["resource_scope"], "https://graph.microsoft.com/.default")
        factory.return_value.get_token.assert_not_called()


class RuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "PURVIEW_ENABLED": "false", "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false",
            "A365_TENANT_ID": "00000000-0000-4000-8000-000000000006",
            "A365_BLUEPRINT_CLIENT_ID": BLUEPRINT_ID,
            "API_ALLOWED_CLIENT_IDS": json.dumps([APPLICATION_ID]),
            "API_AUTH_PROVIDER": "entra"}))
        self.stack.enter_context(patch.dict(sys.modules, {
            "purview": purview, "observability": SimpleNamespace(configure=Mock())}))
        original_read = pathlib.Path.read_text
        manifest = {
            "agentName": "synthetic-agent", "displayName": "Synthetic Agent",
            "purpose": "Classify synthetic test content.", "writes": False,
        }

        def read_manifest(path, *arguments, **keywords):
            if path.name == "agent_manifest.json" and path.parent == MODULE_PATH.parent:
                return json.dumps(manifest)
            return original_read(path, *arguments, **keywords)

        def load_module(name, filename):
            spec = importlib.util.spec_from_file_location(name, MODULE_PATH.parent / filename)
            module = importlib.util.module_from_spec(spec)
            self.stack.enter_context(patch.dict(sys.modules, {name: module}))
            spec.loader.exec_module(module)
            return module

        with patch.object(pathlib.Path, "read_text", autospec=True, side_effect=read_manifest):
            authentication = load_module("authentication", "authentication.py")
            load_module("delegation", "delegation.py")
            self.graph = load_module("graph", "graph.py")
            self.main = load_module("purview_runtime_test", "main.py")
        self.caller = authentication.Caller(
            "00000000-0000-4000-8000-000000000006", AGENT_USER_ID, APPLICATION_ID,
            "user", 4102444800, "synthetic-user-assertion")
        self.stack.enter_context(patch.object(self.main._auth, "authenticate_token", return_value=self.caller))
        self.policy = Mock(check_output=False)
        self.main._purview = self.policy
        self.main._graph = self.graph.build_graph(self.policy)
        self.model = Mock()
        self.model.invoke.return_value = SimpleNamespace(
            content='{"category":"synthetic-category","urgency":"baixa"}', response_metadata={})
        self.model_factory = self.stack.enter_context(patch.object(self.graph, "_chat_model", return_value=self.model))
        self.client = self.stack.enter_context(TestClient(self.main.app))
        self.client.headers["Authorization"] = "Bearer synthetic-client-token"

    def block_at(self, checkpoint, exception_type=purview.PurviewBlockedError):
        def evaluate(content, **context):
            if context["checkpoint"] == checkpoint:
                raise exception_type("PURVIEW_DLP_BLOCKED")
        self.policy.check_text.side_effect = evaluate

    def test_unauthenticated_invocation_stops_before_policy_and_model(self):
        self.client.headers.pop("Authorization")
        response = self.client.post("/invoke", json={"input": "synthetic input"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.policy.check_text.assert_not_called()
        self.model_factory.assert_not_called()

    def test_hosting_headers_do_not_replace_api_authentication(self):
        self.client.headers.pop("Authorization")
        response = self.client.post("/invoke", json={"input": "synthetic input"}, headers={
            "X-MS-CLIENT-PRINCIPAL": "untrusted", "X-MS-TOKEN-AAD-ACCESS-TOKEN": "untrusted"})
        self.assertEqual(response.status_code, 401)
        self.policy.check_text.assert_not_called()
        self.model_factory.assert_not_called()

    def test_only_health_is_public(self):
        self.client.headers.pop("Authorization")
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/manifest").status_code, 401)
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)

    def test_invalid_forbidden_and_unavailable_auth_stop_before_processing(self):
        for exception, status_code in (
                (self.main.authentication.InvalidAuthentication, 401),
                (self.main.authentication.ForbiddenCaller, 403),
                (self.main.authentication.AuthenticationUnavailable, 503)):
            with self.subTest(status=status_code):
                self.main._auth.authenticate_token.side_effect = exception("private-token-detail")
                response = self.client.post("/invoke", json={"input": "synthetic input"})
                self.assertEqual(response.status_code, status_code)
                self.assertNotIn("private-token-detail", response.text)
        self.policy.check_text.assert_not_called()
        self.model_factory.assert_not_called()

    def test_direct_client_uses_signed_access_token_without_console(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.main._auth._keys = Mock()
        self.main._auth._keys.get_signing_key_from_jwt.return_value = SimpleNamespace(key=key.public_key())
        self.main._auth.authenticate_token.side_effect = lambda token: self.main.authentication.EntraAuthenticator.authenticate_token(self.main._auth, token)
        now = int(time.time())
        token = jwt.encode({
            'aud': BLUEPRINT_ID, 'iss': self.main._auth.issuer, 'tid': self.main._auth.tenant_id,
            'exp': now + 600, 'nbf': now - 10, 'iat': now - 10, 'ver': '2.0',
            'oid': AGENT_USER_ID, 'sub': 'synthetic-user', 'azp': APPLICATION_ID, 'scp': 'Agent.Invoke',
        }, key, algorithm='RS256', headers={'kid': 'synthetic-key'})
        response = self.client.post('/invoke', json={'input': 'synthetic input'},
                                    headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(token, response.text)
        self.assertNotIn(token, str(self.policy.check_text.call_args_list))

    def test_obo_uses_verified_caller_and_preserves_policy_subject(self):
        provider = Mock()
        provider.read_profile.return_value = {'id': AGENT_USER_ID, 'displayName': 'Synthetic User'}
        self.main._delegation = provider
        response = self.client.post('/invoke', json={'input': 'synthetic input', 'userId': 'untrusted'})
        self.assertEqual(response.status_code, 200)
        provider.read_profile.assert_called_once_with(self.caller)
        self.assertEqual([call.kwargs['checkpoint'] for call in self.policy.check_text.call_args_list],
                         ['prompt', 'tool_response'])
        self.assertNotIn(self.caller.user_assertion, response.text)
        self.assertNotIn(self.caller.user_assertion, str(self.policy.check_text.call_args_list))

    def test_obo_application_caller_stops_before_policy_and_model(self):
        self.main._delegation = Mock()
        self.main._auth.authenticate_token.return_value = replace(self.caller, kind='application', user_assertion=None)
        response = self.client.post('/invoke', json={'input': 'synthetic input'})
        self.assertEqual(response.status_code, 403)
        self.main._delegation.read_profile.assert_not_called()
        self.policy.check_text.assert_not_called()
        self.model_factory.assert_not_called()

    def test_obo_prompt_block_prevents_delegated_access(self):
        self.main._delegation = Mock()
        self.block_at('prompt')
        response = self.client.post('/invoke', json={'input': 'sensitive input'})
        self.assertEqual(response.status_code, 403)
        self.main._delegation.read_profile.assert_not_called()
        self.model_factory.assert_not_called()

    def test_obo_tool_block_hides_delegated_profile(self):
        self.main._delegation = Mock()
        self.main._delegation.read_profile.return_value = {'displayName': 'sensitive-profile'}
        self.block_at('tool_response')
        response = self.client.post('/invoke', json={'input': 'synthetic input'})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('sensitive-profile', response.text)

    def test_obo_challenge_is_returned_without_fallback(self):
        self.main._delegation = Mock()
        error = self.main.delegation.interaction_required('{"access_token":{"acrs":{"value":"c1"}}}')
        self.main._delegation.read_profile.side_effect = error
        response = self.client.post('/invoke', json={'input': 'synthetic input'})
        self.assertEqual(response.status_code, 401)
        self.assertIn('insufficient_claims', response.headers['WWW-Authenticate'])
        self.main._delegation.read_profile.assert_called_once()

    def test_obo_credentials_are_not_graph_state(self):
        self.main._delegation = Mock()
        with patch.object(self.main, 'build_graph') as factory:
            factory.return_value.invoke.return_value = {
                'output': 'synthetic output', 'category': 'synthetic', 'urgency': 'baixa', 'steps': []}
            response = self.client.post('/invoke', json={'input': 'synthetic input'})
        self.assertEqual(response.status_code, 200)
        state = factory.return_value.invoke.call_args.args[0]
        self.assertEqual(set(state), {'input', 'steps', 'correlation_id'})
        self.assertNotIn(self.caller.user_assertion, str(state))

    def test_prompt_block_does_not_invoke_model_or_graph(self):
        self.block_at("prompt")
        with patch.object(self.main._graph, "invoke") as invoke:
            response = self.client.post("/invoke", json={"input": "sensitive-input"})
        self.assertEqual(response.status_code, 403)
        invoke.assert_not_called()
        self.model_factory.assert_not_called()
        self.assertNotIn("sensitive-input", response.text)

    def test_tool_content_block_is_not_returned(self):
        self.block_at("tool_response")
        response = self.client.post("/invoke", json={"input": "synthetic input"})
        self.assertEqual(response.status_code, 403)
        self.model.invoke.assert_called_once()
        self.assertNotIn("synthetic-category", response.text)
        self.assertEqual([call.kwargs["checkpoint"] for call in self.policy.check_text.call_args_list],
                         ["prompt", "tool_response"])

    def test_evaluation_failure_stops_before_model(self):
        self.block_at("prompt", purview.PurviewUnavailableError)
        response = self.client.post("/invoke", json={"input": "synthetic input"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "PURVIEW_EVALUATION_UNAVAILABLE")
        self.model_factory.assert_not_called()

    def test_allowed_flow_preserves_existing_response_and_context(self):
        response = self.client.post("/invoke", json={"input": "synthetic input", "userId": "untrusted-user"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["steps"], ["classify", "act"])
        calls = self.policy.check_text.call_args_list
        self.assertEqual(calls[0].kwargs["correlation_id"], calls[1].kwargs["correlation_id"])
        self.assertEqual([call.kwargs["sequence_number"] for call in calls], [0, 1])
        self.assertNotIn("untrusted-user", str(calls))

    def test_optional_output_block_hides_the_whole_response(self):
        self.policy.check_output = True
        self.block_at("agent_response")
        response = self.client.post("/invoke", json={"input": "synthetic input"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("synthetic-category", response.text)
        self.assertEqual(self.policy.check_text.call_args.kwargs["activity"], "downloadText")

    def test_disabled_feature_preserves_existing_flow(self):
        self.main._purview = None
        self.main._graph = self.graph.build_graph()
        response = self.client.post("/invoke", json={"input": "synthetic input"})
        self.assertEqual(response.status_code, 200)
        self.policy.check_text.assert_not_called()

    def test_graph_decisions_enforce_real_runtime_boundaries(self):
        for blocked_checkpoint, expected_status, expected_model_calls in (
                (None, 200, 1), ("prompt", 403, 0), ("tool_response", 403, 1)):
            with self.subTest(checkpoint=blocked_checkpoint):
                checkpoints = []

                def handle(request):
                    self.assertIn(f"/users/{AGENT_USER_ID}/", request.url.path)
                    if request.url.path.endswith("/compute"):
                        return httpx.Response(200, headers={"ETag": '"scope-v1"'}, json={"value": [{
                            "activities": "uploadText",
                            "executionMode": "evaluateInline",
                            "locations": [{"value": APPLICATION_ID}],
                            "policyActions": [],
                        }]})
                    entry = json.loads(request.content)["contentToProcess"]["contentEntries"][0]
                    checkpoints.append(entry["name"])
                    return httpx.Response(200, json={
                        "protectionScopeState": "notModified",
                        "policyActions": [BLOCK_ACTION] if entry["name"] == blocked_checkpoint else [],
                        "processingErrors": [],
                    })

                with httpx.Client(transport=httpx.MockTransport(handle)) as graph_client:
                    policy = purview.PurviewPolicyClient(
                        agent_user_id=AGENT_USER_ID, application_id=APPLICATION_ID,
                        agent_id=AGENT_ID, blueprint_id=BLUEPRINT_ID, agent_name="Synthetic Agent",
                        token_provider=lambda: "synthetic-token", client=graph_client)
                    self.main._purview = policy
                    self.main._graph = self.graph.build_graph(policy)
                    self.model.invoke.reset_mock()
                    response = self.client.post("/invoke", json={"input": "synthetic input"})
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(self.model.invoke.call_count, expected_model_calls)
                self.assertEqual(checkpoints, ["prompt"] if blocked_checkpoint == "prompt"
                                 else ["prompt", "tool_response"])
                if expected_status == 403:
                    self.assertNotIn("synthetic-category", response.text)


if __name__ == "__main__":
    unittest.main()