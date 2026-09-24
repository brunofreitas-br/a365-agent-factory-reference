"""Superfície HTTP do agente, para rodar em Azure Container Apps.

A observabilidade é configurada ANTES de importar o grafo: o módulo do grafo pega
um tracer no import, e um tracer obtido antes do provider global existir vira no-op.
"""
from __future__ import annotations

import json
import logging
import pathlib
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from pydantic import BaseModel, Field

import observability
import purview

logging.basicConfig(level=logging.INFO)

MANIFEST = json.loads(
    (pathlib.Path(__file__).parent / "agent_manifest.json").read_text(encoding="utf-8")
)

observability.configure(service_name=MANIFEST["agentName"])

from graph import build_graph  # noqa: E402  (precisa vir depois do configure)

_purview = purview.configure(MANIFEST)


@asynccontextmanager
async def lifespan(application: FastAPI):
    try:
        yield
    finally:
        if _purview is not None:
            _purview.close()


app = FastAPI(title=MANIFEST["displayName"], version="1.0.0", lifespan=lifespan)
_graph = build_graph(_purview)
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
    correlation_id = str(uuid.uuid4())
    try:
        with tracer.start_as_current_span("invoke_agent") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", MANIFEST["agentName"])
            span.set_attribute("gen_ai.conversation.id", correlation_id)
            span.set_attribute("purview.enabled", _purview is not None)
            if _purview is not None:
                _purview.check_text(
                    request.input, activity="uploadText", checkpoint="prompt",
                    correlation_id=correlation_id, sequence_number=0)

            result = _graph.invoke({
                "input": request.input, "steps": [], "correlation_id": correlation_id})

            response = InvokeResponse(
                output=result["output"],
                category=result["category"],
                urgency=result["urgency"],
                steps=result["steps"],
            )
            if _purview is not None and _purview.check_output:
                _purview.check_text(
                    response.model_dump_json(), activity="downloadText", checkpoint="agent_response",
                    correlation_id=correlation_id, sequence_number=2)
            return response
    except purview.PurviewBlockedError as error:
        raise HTTPException(status_code=403, detail={
            "code": str(error), "message": "Conteudo bloqueado pela politica de protecao de dados do agente."}) from None
    except purview.PurviewUnavailableError:
        raise HTTPException(status_code=503, detail={
            "code": "PURVIEW_EVALUATION_UNAVAILABLE",
            "message": "A avaliacao obrigatoria de protecao de dados nao pode ser concluida."}) from None
