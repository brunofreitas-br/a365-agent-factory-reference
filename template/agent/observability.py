"""Observabilidade A365 — exportação OTLP para o endpoint S2S do Agent 365.

Caminho de token (2 hops, VERIFICADO com span real no tenant):

    1. FIC       client_credentials + fmi_path=<instanceId>
                 scope api://AzureAdTokenExchange/.default
    2. Instance  client_credentials + client_assertion=<token do hop 1>
                 scope api://9b975845-388f-4429-889e-eab1ef63949c/.default

O token do hop 2 **não tem claim `roles`, `scp` nem `appid`** — a autorização vem do
caminho FMI, não de um app role atribuído. Não é preciso criar appRoleAssignment para
a instância: as permissões herdáveis que o `a365 setup all` configura no blueprint já
bastam (Observability API com `Agent365.Observability.OtelWrite`).

Use SEMPRE o endpoint `/observabilityService/`. O `/observability/` recusa este tipo de
token com 401 `UnsupportedAccessTokenType` — é incompatibilidade de tipo de token, não
falta de permissão.

Resposta de sucesso (HTTP 200) mostra em quais sinks o span pousou:

    {"results":[{"spanId":"...","sinks":{"flashpoint":{"status":"sent"},
     "sentinel":{"status":"sent"},"esp":{"status":"sent"}}}],
     "partialSuccess":{"rejectedSpans":0,"errorMessage":""}}

O endpoint aceita OTLP/JSON além de protobuf — útil para diagnóstico sem dependências
(ver scripts/smoke_observability.py).

Nota de evolução: o destino é o SDK oficial (microsoft-agents-a365-observability-core),
gerado pela skill `instrument-observability`. Os dois convergem no mesmo endpoint e no
mesmo token — este módulo existe para o lab rodar hoje, sem esperar a fiação do SDK.
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
    """Exporta OTLP/JSON, não protobuf.

    O exporter protobuf padrão do OTel devolve 403 neste endpoint; o mesmo span em JSON
    devolve 200 com `rejectedSpans: 0`. Como o formato JSON está verificado ponta a ponta
    (ver scripts/smoke_observability.py), é ele que usamos aqui.
    """

    _KIND = {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3, "PRODUCER": 4, "CONSUMER": 5}

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
        except Exception:
            log.exception("Falha ao exportar spans para o A365.")
            return SpanExportResult.FAILURE
        if response.status_code != 200:
            log.error("A365 recusou o lote de spans: HTTP %s %s",
                      response.status_code, response.text[:300])
            return SpanExportResult.FAILURE

        # HTTP 200 só diz que o serviço recebeu. Quantos ele aceitou está no corpo,
        # e um 200 com spans rejeitados é o silêncio mais caro dessa integração.
        try:
            parcial = (response.json() or {}).get("partialSuccess") or {}
        except ValueError:
            log.warning("A365 devolveu 200 com corpo não-JSON: %s", response.text[:200])
            return SpanExportResult.SUCCESS

        rejeitados = parcial.get("rejectedSpans", 0) or 0
        mensagem = parcial.get("errorMessage") or ""
        if rejeitados:
            log.error("A365 aceitou %s de %s span(s). Rejeitados: %s. Motivo: %s",
                      len(spans) - rejeitados, len(spans), rejeitados, mensagem or "(sem detalhe)")
            return SpanExportResult.FAILURE
        if mensagem:
            log.warning("A365 aceitou todos os spans com aviso: %s", mensagem)
        log.info("A365 aceitou %s span(s), 0 rejeitado(s).", len(spans))
        return SpanExportResult.SUCCESS

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
