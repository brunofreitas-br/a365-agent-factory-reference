"""Observabilidade A365 com OTLP/JSON e autenticação S2S derivada do blueprint.

A cadeia usa duas trocas de token. O contrato S2S documentado exige a identidade
runtime e o app role Agent365.Observability.OtelWrite no token final.

HTTP 200 confirma processamento, não entrega. Avaliamos partialSuccess e results;
roteamento confirmado ainda exige verificar a indexação no destino consumidor.
O exportador não reenvia lotes, inclusive quando há entrega parcial.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import httpx
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (BatchSpanProcessor, SpanExporter,
                                            SpanExportResult)

log = logging.getLogger(__name__)

# App ID fixo do serviço Agent365Observability.
OBSERVABILITY_APP_ID = "9b975845-388f-4429-889e-eab1ef63949c"
OBSERVABILITY_SCOPE = f"api://{OBSERVABILITY_APP_ID}/.default"
TOKEN_EXCHANGE_SCOPE = "api://AzureAdTokenExchange/.default"
DEFAULT_ENDPOINT = "https://agent365.svc.cloud.microsoft"

# Renova antes do vencimento real (tokens Entra duram ~60 min).
_REFRESH_MARGIN_SECONDS = 600


class A365TokenService:
    """Cadeia FIC de 2 hops, com cache em memória e renovação preguiçosa."""

    def __init__(self, tenant_id: str, blueprint_client_id: str,
                 blueprint_client_secret: str, instance_id: str):
        self._tenant_id = tenant_id
        self._blueprint_client_id = blueprint_client_id
        self._blueprint_client_secret = blueprint_client_secret
        self._instance_id = instance_id
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()
        self._url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    def _post(self, data: dict[str, str]) -> dict:
        response = httpx.post(self._url, data=data, timeout=30.0)
        if response.status_code != 200:
            # Não logar o corpo inteiro: pode conter fragmentos de assertion.
            raise RuntimeError(
                f"Falha no token endpoint ({response.status_code}): "
                f"{response.json().get('error_description', '')[:200]}"
            )
        return response.json()

    def _fetch(self) -> tuple[str, float]:
        fic = self._post({
            "grant_type": "client_credentials",
            "client_id": self._blueprint_client_id,
            "client_secret": self._blueprint_client_secret,
            "scope": TOKEN_EXCHANGE_SCOPE,
            "fmi_path": self._instance_id,
        })
        instance = self._post({
            "grant_type": "client_credentials",
            "client_id": self._instance_id,
            "client_assertion": fic["access_token"],
            "client_assertion_type":
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "scope": OBSERVABILITY_SCOPE,
        })
        expires_in = int(instance.get("expires_in", 3600))
        return instance["access_token"], time.time() + expires_in

    def get_token(self) -> str:
        """Retorna o token CRU. Quem chama adiciona 'Bearer ' — nunca este método."""
        with self._lock:
            if self._token and time.time() < self._expires_at - _REFRESH_MARGIN_SECONDS:
                return self._token
            self._token, self._expires_at = self._fetch()
            log.info("Token de observabilidade A365 renovado.")
            return self._token


class _A365JsonSpanExporter(SpanExporter):
    """Exporta OTLP/JSON e verifica os recibos de roteamento por span e destino."""

    _KIND = {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3, "PRODUCER": 4, "CONSUMER": 5}
    _SAFE_REASONS = frozenset({"tenant_not_licensed"})

    def __init__(self, endpoint: str, token_service: A365TokenService, service_name: str):
        self._endpoint = endpoint
        self._token_service = token_service
        self._service_name = service_name

    @staticmethod
    def _attr(key, value) -> dict:
        if isinstance(value, bool):
            return {"key": key, "value": {"boolValue": value}}
        if isinstance(value, int):
            return {"key": key, "value": {"intValue": str(value)}}
        if isinstance(value, float):
            return {"key": key, "value": {"doubleValue": value}}
        return {"key": key, "value": {"stringValue": str(value)}}

    def _span_json(self, span) -> dict:
        ctx = span.get_span_context()
        out = {
            "traceId": format(ctx.trace_id, "032x"),
            "spanId": format(ctx.span_id, "016x"),
            "name": span.name,
            "kind": self._KIND.get(span.kind.name, 1),
            "startTimeUnixNano": str(span.start_time),
            "endTimeUnixNano": str(span.end_time),
            "attributes": [self._attr(k, v) for k, v in (span.attributes or {}).items()],
            "status": {"code": 2 if span.status.status_code.name == "ERROR" else 1},
        }
        if span.parent is not None:
            out["parentSpanId"] = format(span.parent.span_id, "016x")
        return out

    def export(self, spans) -> SpanExportResult:
        if not spans:
            return SpanExportResult.SUCCESS
        payload = {"resourceSpans": [{
            "resource": {"attributes": [self._attr("service.name", self._service_name)]},
            "scopeSpans": [{"scope": {"name": "agent-factory"},
                            "spans": [self._span_json(s) for s in spans]}],
        }]}
        try:
            response = httpx.post(
                self._endpoint, json=payload, timeout=30.0,
                headers={"Authorization": f"Bearer {self._token_service.get_token()}"})
        except Exception as error:
            log.error("A365 export: routing=unconfirmed exception=%s", type(error).__name__)
            return SpanExportResult.FAILURE
        if response.status_code != 200:
            log.error("A365 export: http=%s routing=unconfirmed", response.status_code)
            return SpanExportResult.FAILURE

        try:
            body = response.json()
        except ValueError:
            log.error("A365 export: http=200 routing=unconfirmed response=invalid_json")
            return SpanExportResult.FAILURE
        return self._evaluate_response(body, spans)

    def _evaluate_response(self, body, spans) -> SpanExportResult:
        if not isinstance(body, dict):
            log.error("A365 export: http=200 routing=unconfirmed response=invalid_body")
            return SpanExportResult.FAILURE
        partial = body.get("partialSuccess")
        if partial is None:
            partial = {}
        if not isinstance(partial, dict):
            log.error("A365 export: http=200 routing=unconfirmed response=invalid_partial")
            return SpanExportResult.FAILURE
        rejected_count = partial.get("rejectedSpans", 0)
        if (isinstance(rejected_count, str) and len(rejected_count) <= 20
                and rejected_count.isascii() and rejected_count.isdecimal()):
            rejected_count = int(rejected_count)
        if type(rejected_count) is not int or not 0 <= rejected_count <= len(spans):
            log.error("A365 export: http=200 routing=unconfirmed response=invalid_count")
            return SpanExportResult.FAILURE
        results = body.get("results")
        if not isinstance(results, list) or not results:
            log.error("A365 export: http=200 routing=unconfirmed response=missing_results")
            return SpanExportResult.FAILURE

        expected_ids = {format(span.get_span_context().span_id, "016x") for span in spans}
        received_ids = set()
        totals = {"sent": 0, "rejected": 0, "not_routed": 0}
        destinations = {}
        routed_spans = 0
        for result in results:
            if not isinstance(result, dict):
                log.error("A365 export: http=200 routing=unconfirmed response=invalid_result")
                return SpanExportResult.FAILURE
            span_id = result.get("spanId")
            if (not isinstance(span_id, str) or span_id not in expected_ids
                    or span_id in received_ids):
                log.error("A365 export: http=200 routing=unconfirmed response=span_mismatch")
                return SpanExportResult.FAILURE
            received_ids.add(span_id)
            sinks = result.get("sinks")
            if not isinstance(sinks, dict) or not sinks:
                log.error("A365 export: http=200 routing=unconfirmed response=missing_destinations")
                return SpanExportResult.FAILURE
            span_sent = False
            for destination, receipt in sinks.items():
                receipt_status = receipt.get("status") if isinstance(receipt, dict) else None
                if not isinstance(receipt_status, str) or receipt_status not in totals:
                    log.error("A365 export: http=200 routing=unconfirmed response=invalid_status")
                    return SpanExportResult.FAILURE
                counts = destinations.setdefault(
                    destination, {"sent": 0, "rejected": 0, "not_routed": 0, "reasons": set()})
                counts[receipt_status] += 1
                totals[receipt_status] += 1
                span_sent = span_sent or receipt_status == "sent"
                reason = receipt.get("reason")
                if reason:
                    safe_reason = (reason if isinstance(reason, str) and reason in self._SAFE_REASONS
                                   else "redacted")
                    counts["reasons"].add(safe_reason)
            routed_spans += int(span_sent)

        for index, destination in enumerate(sorted(destinations), start=1):
            counts = destinations[destination]
            log.info("A365 destination_%s: sent=%s rejected=%s not_routed=%s reasons=%s",
                     index, counts["sent"], counts["rejected"], counts["not_routed"],
                     ",".join(sorted(counts["reasons"])) or "none")

        failed = bool(rejected_count or totals["rejected"] or received_ids != expected_ids
                      or routed_spans != len(spans))
        routing = "partial" if routed_spans else "unconfirmed"
        if not failed and not totals["not_routed"]:
            routing = "confirmed"
        message_present = bool(partial.get("errorMessage"))
        level = logging.ERROR if failed else (
            logging.WARNING if totals["not_routed"] or message_present else logging.INFO)
        log.log(level, "A365 export: http=200 routing=%s spans=%s receipts=%s "
                "rejected_spans=%s sent=%s rejected=%s not_routed=%s errorMessage_present=%s",
                routing, len(spans), len(received_ids), rejected_count, totals["sent"],
                totals["rejected"], totals["not_routed"], message_present)
        return SpanExportResult.FAILURE if failed else SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


class _A365AttributeProcessor(SpanProcessor):
    """Carimba tenant e agent id em todo span.

    Feito no on_start em vez de via BaggageBuilder: baggage não propaga de forma
    confiável entre tarefas async em Python.
    """

    def __init__(self, tenant_id: str, agent_id: str):
        self._attrs = {"microsoft.tenant.id": tenant_id, "gen_ai.agent.id": agent_id}

    def on_start(self, span, parent_context: Context | None = None) -> None:
        span.set_attributes(self._attrs)

    def on_end(self, span) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def configure(service_name: str) -> trace.Tracer:
    """Configura o tracer global. Idempotente por processo.

    Se as variáveis de ambiente do A365 não estiverem presentes, cai para tracing
    local (sem exportação) em vez de derrubar o agente — telemetria não deve ser
    caminho crítico.
    """
    tenant_id = os.getenv("A365_TENANT_ID")
    agent_id = os.getenv("A365_AGENT_INSTANCE_ID")
    blueprint_id = os.getenv("A365_BLUEPRINT_CLIENT_ID")
    blueprint_secret = os.getenv("A365_BLUEPRINT_CLIENT_SECRET")

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if all([tenant_id, agent_id, blueprint_id, blueprint_secret]):
        base = os.getenv("A365_OBSERVABILITY_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")
        # Endpoint S2S: o caminho /observability/ devolve 403 com corpo vazio para
        # tokens de app role; o /observabilityService/ devolve WWW-Authenticate útil.
        endpoint = (f"{base}/observabilityService/tenants/{tenant_id}"
                    f"/otlp/agents/{agent_id}/traces?api-version=1")
        token_service = A365TokenService(tenant_id, blueprint_id, blueprint_secret, agent_id)
        provider.add_span_processor(_A365AttributeProcessor(tenant_id, agent_id))
        provider.add_span_processor(BatchSpanProcessor(
            _A365JsonSpanExporter(endpoint, token_service, service_name)))
        log.info("Observabilidade A365 ativa para o agente %s.", agent_id)
    else:
        log.warning(
            "Variáveis A365 ausentes — tracing local, sem exportação. "
            "Defina A365_TENANT_ID, A365_AGENT_INSTANCE_ID, A365_BLUEPRINT_CLIENT_ID "
            "e A365_BLUEPRINT_CLIENT_SECRET.")

    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)
