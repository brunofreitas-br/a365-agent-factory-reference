import importlib.util
import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from opentelemetry.sdk.trace.export import SpanExportResult


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "template/agent/observability.py"
SPEC = importlib.util.spec_from_file_location("agent_observability", MODULE_PATH)
observability = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observability)


class ExportResponseTests(unittest.TestCase):
    def setUp(self):
        self.span_id = "0000000000000002"
        self.span = SimpleNamespace(
            get_span_context=lambda: SimpleNamespace(trace_id=1, span_id=2),
            name="invoke_agent",
            kind=SimpleNamespace(name="INTERNAL"),
            start_time=100,
            end_time=200,
            attributes={"gen_ai.operation.name": "invoke_agent"},
            status=SimpleNamespace(status_code=SimpleNamespace(name="OK")),
            parent=None,
        )
        self.exporter = observability._A365JsonSpanExporter(
            "https://example.invalid",
            Mock(get_token=Mock(return_value="synthetic-token")),
            "diagnostic-test",
        )

    def response_body(self, status="sent", reason=None, rejected=0):
        destination = {"status": status}
        if reason is not None:
            destination["reason"] = reason
        return {
            "partialSuccess": {"rejectedSpans": rejected, "errorMessage": ""},
            "results": [{"spanId": self.span_id, "sinks": {"test_destination": destination}}],
        }

    def export_response(self, response):
        with patch.object(observability.httpx, "post", return_value=response) as post:
            with self.assertLogs(observability.log, level="INFO") as captured:
                result = self.exporter.export([self.span])
        post.assert_called_once()
        return result, "\n".join(captured.output)

    def test_sent_results_confirm_routing(self):
        result, logs = self.export_response(httpx.Response(200, json=self.response_body()))
        self.assertEqual(result, SpanExportResult.SUCCESS)
        self.assertIn("routing=confirmed", logs)

    def test_destination_rejection_is_not_success(self):
        body = self.response_body("rejected", "tenant_not_licensed")
        result, logs = self.export_response(httpx.Response(200, json=body))
        self.assertEqual(result, SpanExportResult.FAILURE)
        self.assertIn("tenant_not_licensed", logs)
        self.assertNotIn("routing=confirmed", logs)

    def test_no_destination_selected_is_unconfirmed(self):
        result, logs = self.export_response(
            httpx.Response(200, json=self.response_body("not_routed"))
        )
        self.assertEqual(result, SpanExportResult.FAILURE)
        self.assertIn("not_routed=1", logs)

    def test_not_selected_destination_with_delivery_is_distinguished(self):
        body = self.response_body()
        body["results"][0]["sinks"]["other_destination"] = {"status": "not_routed"}
        result, logs = self.export_response(httpx.Response(200, json=body))
        self.assertEqual(result, SpanExportResult.SUCCESS)
        self.assertIn("routing=partial", logs)
        self.assertNotIn("routing=confirmed", logs)

    def test_partial_destination_rejection_does_not_retry(self):
        body = self.response_body()
        body["results"][0]["sinks"]["other_destination"] = {
            "status": "rejected", "reason": "tenant_not_licensed"
        }
        result, logs = self.export_response(httpx.Response(200, json=body))
        self.assertEqual(result, SpanExportResult.FAILURE)
        self.assertNotIn("routing=confirmed", logs)

    def test_numeric_and_string_rejected_counts(self):
        for count, expected in ((0, SpanExportResult.SUCCESS), ("0", SpanExportResult.SUCCESS),
                                (1, SpanExportResult.FAILURE), ("1", SpanExportResult.FAILURE)):
            with self.subTest(count=count):
                result, _ = self.export_response(
                    httpx.Response(200, json=self.response_body(rejected=count))
                )
                self.assertEqual(result, expected)

    def test_invalid_counts_do_not_escape_export(self):
        for count in (-1, "-1", "invalid", True, 0.5, [], {}, 2):
            with self.subTest(count=count):
                result, logs = self.export_response(
                    httpx.Response(200, json=self.response_body(rejected=count))
                )
                self.assertEqual(result, SpanExportResult.FAILURE)
                self.assertNotIn("routing=confirmed", logs)

    def test_missing_or_malformed_response_never_confirms_delivery(self):
        for body in ({}, None, [], {"partialSuccess": {}}, {"results": []},
                     {"partialSuccess": "invalid"}, {"results": "invalid"}):
            with self.subTest(body=body):
                result, logs = self.export_response(httpx.Response(200, json=body))
                self.assertEqual(result, SpanExportResult.FAILURE)
                self.assertNotIn("routing=confirmed", logs)

    def test_non_json_body_is_not_success_or_logged(self):
        result, logs = self.export_response(httpx.Response(200, text="private-response-body"))
        self.assertEqual(result, SpanExportResult.FAILURE)
        self.assertNotIn("private-response-body", logs)

    def test_result_must_match_submitted_spans(self):
        bodies = []
        body = self.response_body()
        body["results"][0]["spanId"] = "0000000000000003"
        bodies.append(body)
        body = self.response_body()
        body["results"].append(body["results"][0])
        bodies.append(body)
        for body in bodies:
            with self.subTest(body=body):
                result, logs = self.export_response(httpx.Response(200, json=body))
                self.assertEqual(result, SpanExportResult.FAILURE)
                self.assertNotIn("routing=confirmed", logs)

    def test_invalid_destination_receipts_are_not_success(self):
        for sinks in ({}, [], {"test_destination": None},
                      {"test_destination": {"status": "unexpected"}}):
            body = self.response_body()
            body["results"][0]["sinks"] = sinks
            with self.subTest(sinks=sinks):
                result, _ = self.export_response(httpx.Response(200, json=body))
                self.assertEqual(result, SpanExportResult.FAILURE)

    def test_free_text_and_identifiers_are_not_logged(self):
        body = self.response_body("rejected", "private-token-123\nextra-log-line")
        body["partialSuccess"]["errorMessage"] = "private-prompt-and-token"
        result, logs = self.export_response(httpx.Response(200, json=body))
        self.assertEqual(result, SpanExportResult.FAILURE)
        for private_value in ("test_destination", "private-token", "extra-log-line",
                              "private-prompt", self.span_id, "synthetic-token"):
            self.assertNotIn(private_value, logs)
        self.assertIn("errorMessage_present=True", logs)

    def test_http_errors_never_log_the_body(self):
        for status_code in (401, 403, 429, 500):
            with self.subTest(status_code=status_code):
                result, logs = self.export_response(
                    httpx.Response(status_code, text="private-response-body")
                )
                self.assertEqual(result, SpanExportResult.FAILURE)
                self.assertNotIn("private-response-body", logs)

    def test_empty_batch_does_not_send(self):
        with patch.object(observability.httpx, "post") as post:
            self.assertEqual(self.exporter.export([]), SpanExportResult.SUCCESS)
        post.assert_not_called()


class AgentTokenResourceTests(unittest.TestCase):
    def test_default_resource_and_graph_resource_have_separate_caches(self):
        common = ("synthetic-tenant", "synthetic-blueprint", "synthetic-secret", "synthetic-agent")
        telemetry = observability.A365TokenService(*common)
        graph = observability.A365TokenService(*common, resource_scope="https://graph.microsoft.com/.default")
        for service, expected_scope, token in (
                (telemetry, observability.OBSERVABILITY_SCOPE, "synthetic-telemetry-token"),
                (graph, "https://graph.microsoft.com/.default", "synthetic-graph-token")):
            with self.subTest(scope=expected_scope):
                with patch.object(service, "_post", side_effect=[
                        {"access_token": "synthetic-exchange-token"},
                        {"access_token": token, "expires_in": 3600},
                ]) as post:
                    self.assertEqual(service.get_token(), token)
                    self.assertEqual(service.get_token(), token)
                    self.assertEqual(post.call_count, 2)
                    self.assertEqual(post.call_args_list[0].args[0]["scope"], observability.TOKEN_EXCHANGE_SCOPE)
                    self.assertEqual(post.call_args_list[1].args[0]["scope"], expected_scope)
        self.assertNotEqual(telemetry._token, graph._token)


if __name__ == "__main__":
    unittest.main()