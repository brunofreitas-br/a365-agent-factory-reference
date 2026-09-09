"""Superfície HTTP do agente, para rodar em Azure Container Apps.

A observabilidade é configurada ANTES de importar o grafo: o módulo do grafo pega
um tracer no import, e um tracer obtido antes do provider global existir vira no-op.
"""
from __future__ import annotations

import json
import logging
import pathlib

from fastapi import FastAPI
from opentelemetry import trace
from pydantic import BaseModel, Field

import observability

logging.basicConfig(level=logging.INFO)

MANIFEST = json.loads(
    (pathlib.Path(__file__).parent / "agent_manifest.json").read_text(encoding="utf-8")
)

observability.configure(service_name=MANIFEST["agentName"])

from graph import build_graph  # noqa: E402  (precisa vir depois do configure)

app = FastAPI(title=MANIFEST["displayName"], version="1.0.0")
_graph = build_graph()
tracer = trace.get_tracer(__name__)


class InvokeRequest(BaseModel):
    input: str = Field(min_length=1, max_length=8000)


class InvokeResponse(BaseModel):
    output: str
    category: str
    urgency: str
    steps: list[str]


@app.get("/healthz")
def healthz() -> dict:
    """Probe do Container App. Não toca no LLM nem no endpoint de telemetria."""
    return {"status": "ok", "agent": MANIFEST["agentName"]}


@app.get("/manifest")
def manifest() -> dict:
    """A solicitação aprovada, congelada. Serve de evidência em auditoria."""
    return MANIFEST


@app.post("/invoke", response_model=InvokeResponse)
def invoke(request: InvokeRequest) -> InvokeResponse:
    with tracer.start_as_current_span("invoke_agent") as span:
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.agent.name", MANIFEST["agentName"])

        result = _graph.invoke({"input": request.input, "steps": []})

        return InvokeResponse(
            output=result["output"],
            category=result["category"],
            urgency=result["urgency"],
            steps=result["steps"],
        )
