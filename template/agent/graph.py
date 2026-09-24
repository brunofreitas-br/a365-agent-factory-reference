"""Grafo LangGraph do agente.

Deliberadamente pequeno: dois nós e uma ferramenta. O ponto do lab é o caminho de
governança (identidade, telemetria, Gates), não a sofisticação do raciocínio.
Troque `classify` e `act` pelo que o agente real precisa fazer.

Cada nó abre um span com os atributos semânticos gen_ai — é isso que faz o agente
aparecer no Agent Activity / Defender, não a exportação sozinha.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from collections.abc import Callable
from functools import partial
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from opentelemetry import trace

from purview import PurviewPolicyClient

tracer = trace.get_tracer(__name__)

MANIFEST = json.loads(
    (pathlib.Path(__file__).parent / "agent_manifest.json").read_text(encoding="utf-8")
)


class AgentState(TypedDict):
    input: str
    correlation_id: str
    category: str
    urgency: Literal["baixa", "media", "alta"]
    output: str
    steps: Annotated[list[str], lambda a, b: a + b]


def _chat_model():
    """Azure OpenAI por managed identity — sem API key em lugar nenhum.

    `DefaultAzureCredential` só escolhe a identidade atribuída pelo usuário se
    `AZURE_CLIENT_ID` estiver no ambiente; o Bicep injeta esse valor.
    Resolvido tarde para o container subir mesmo sem credencial de LLM.
    """
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from langchain_openai import AzureChatOpenAI

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
    return AzureChatOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_ad_token_provider=token_provider,
        temperature=0,
    )


def classify(state: AgentState, *, purview_client: PurviewPolicyClient | None = None) -> dict:
    with tracer.start_as_current_span("classify") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", "az.ai.openai")

        prompt = (
            f"Você é {MANIFEST['displayName']}. {MANIFEST['purpose']}\n\n"
            "Classifique o texto abaixo. Responda APENAS com JSON no formato "
            '{"category": "...", "urgency": "baixa|media|alta"}.\n\n'
            f"Texto: {state['input']}"
        )
        response = _chat_model().invoke(prompt)
        span.set_attribute("gen_ai.response.model", getattr(response, "response_metadata", {})
                           .get("model_name", "unknown"))

        try:
            # O modelo costuma devolver o JSON dentro de cerca markdown.
            content = str(response.content).strip()
            if content.startswith("```"):
                content = re.sub(r"^```[a-z]*\s*|\s*```$", "", content, flags=re.I)
            parsed = json.loads(content)
            category = str(parsed.get("category", "indefinido"))
            urgency = parsed.get("urgency", "baixa")
        except (json.JSONDecodeError, AttributeError):
            category, urgency = "indefinido", "baixa"
            span.set_attribute("a365.parse_failed", True)

        if urgency not in ("baixa", "media", "alta"):
            urgency = "baixa"

        if purview_client is None:
            span.set_attribute("a365.category", category)
        span.set_attribute("a365.urgency", urgency)
        return {"category": category, "urgency": urgency, "steps": ["classify"]}


def act(state: AgentState, *, purview_client: PurviewPolicyClient | None = None,
    profile_reader: Callable[[], dict] | None = None) -> dict:
    """Onde o efeito colateral aconteceria. Só grava se o manifesto permitir escrita."""
    with tracer.start_as_current_span("act") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "graph_me" if profile_reader is not None else "registrar_classificacao")
        span.set_attribute("a365.writes_allowed", MANIFEST["writes"])

        if profile_reader is not None:
            profile = profile_reader()
            output = json.dumps({"category": state["category"], "urgency": state["urgency"],
                                 "delegatedProfile": profile}, ensure_ascii=False)
        elif not MANIFEST["writes"]:
            span.set_attribute("a365.write_skipped", True)
            output = (f"Classificado como {state['category']} "
                      f"(urgência {state['urgency']}). Escrita não autorizada pelo manifesto.")
        else:
            # Substitua pela chamada real ao sistema de destino.
            output = (f"Classificado como {state['category']} "
                      f"(urgência {state['urgency']}) e registrado.")

        result = {"output": output, "steps": ["act"]}
        if purview_client is not None:
            purview_client.check_text(
                json.dumps(result, ensure_ascii=False), activity="uploadText",
                checkpoint="tool_response", correlation_id=state.get("correlation_id", ""),
                sequence_number=1)
        return result


def build_graph(purview_client: PurviewPolicyClient | None = None, *,
                profile_reader: Callable[[], dict] | None = None):
    graph = StateGraph(AgentState)
    graph.add_node("classify", partial(classify, purview_client=purview_client))
    graph.add_node("act", partial(act, purview_client=purview_client, profile_reader=profile_reader))
    graph.add_edge(START, "classify")
    graph.add_edge("classify", "act")
    graph.add_edge("act", END)
    return graph.compile()
