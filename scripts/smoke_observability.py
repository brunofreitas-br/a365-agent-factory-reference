#!/usr/bin/env python3
"""Smoke test da observabilidade A365: manda UM span e mostra onde ele pousou.

Sem dependências além da stdlib — de propósito. Quando a telemetria não chega, a
primeira coisa a descartar é a instrumentação do agente; este script tira o agente,
o LangGraph e o SDK do caminho e testa só o par token + endpoint.

Uso:
    python scripts/smoke_observability.py --config /caminho/a365.generated.config.json

Ou por variáveis de ambiente:
    A365_TENANT_ID, A365_AGENT_INSTANCE_ID, A365_BLUEPRINT_CLIENT_ID,
    A365_BLUEPRINT_CLIENT_SECRET

Resposta esperada (HTTP 200):
    {"results":[{"spanId":"...","sinks":{"flashpoint":{"status":"sent"},
     "sentinel":{"status":"sent"},"esp":{"status":"sent"}}}],
     "partialSuccess":{"rejectedSpans":0,"errorMessage":""}}

`rejectedSpans: 0` e os sinks em `sent` significam que o span entrou no pipeline —
inclusive no Sentinel. Se vier 200 mas com rejectedSpans > 0, o problema é o formato
do span, não a autenticação.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OBSERVABILITY_APP_ID = "9b975845-388f-4429-889e-eab1ef63949c"
DEFAULT_HOST = "https://agent365.svc.cloud.microsoft"


def load_settings(config_path: pathlib.Path | None) -> dict:
    if config_path and config_path.exists():
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        return {
            "tenant": cfg["tenantId"],
            "blueprint": cfg["agentBlueprintId"],
            "secret": cfg["agentBlueprintClientSecret"],
            "instance": cfg["AgenticAppId"],
        }
    missing = [k for k in ("A365_TENANT_ID", "A365_AGENT_INSTANCE_ID",
                           "A365_BLUEPRINT_CLIENT_ID", "A365_BLUEPRINT_CLIENT_SECRET")
               if not os.getenv(k)]
    if missing:
        raise SystemExit("Faltam variáveis: " + ", ".join(missing))
    return {
        "tenant": os.environ["A365_TENANT_ID"],
        "blueprint": os.environ["A365_BLUEPRINT_CLIENT_ID"],
        "secret": os.environ["A365_BLUEPRINT_CLIENT_SECRET"],
        "instance": os.environ["A365_AGENT_INSTANCE_ID"],
    }


def post_form(url: str, data: dict) -> dict:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode())
        raise SystemExit(f"Token falhou ({exc.code}): {body.get('error')} :: "
                         f"{body.get('error_description', '')[:200]}")


def acquire_token(s: dict) -> str:
    url = f"https://login.microsoftonline.com/{s['tenant']}/oauth2/v2.0/token"
    fic = post_form(url, {
        "grant_type": "client_credentials",
        "client_id": s["blueprint"],
        "client_secret": s["secret"],
        "scope": "api://AzureAdTokenExchange/.default",
        "fmi_path": s["instance"],
    })
    instance = post_form(url, {
        "grant_type": "client_credentials",
        "client_id": s["instance"],
        "client_assertion": fic["access_token"],
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "scope": f"api://{OBSERVABILITY_APP_ID}/.default",
    })
    return instance["access_token"]


def build_span(s: dict) -> dict:
    now = time.time_ns()
    return {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "a365-smoketest"}}]},
        "scopeSpans": [{
            "scope": {"name": "agent-factory-smoketest"},
            "spans": [{
                "traceId": secrets.token_hex(16),
                "spanId": secrets.token_hex(8),
                "name": "invoke_agent",
                "kind": 1,
                "startTimeUnixNano": str(now - 1_000_000_000),
                "endTimeUnixNano": str(now),
                "attributes": [
                    {"key": "microsoft.tenant.id", "value": {"stringValue": s["tenant"]}},
                    {"key": "gen_ai.agent.id", "value": {"stringValue": s["instance"]}},
                    {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
                ],
                "status": {"code": 1},
            }],
        }],
    }]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=pathlib.Path,
                    help="a365.generated.config.json (contém o segredo do blueprint)")
    ap.add_argument("--host", default=os.getenv("A365_OBSERVABILITY_ENDPOINT", DEFAULT_HOST))
    args = ap.parse_args()

    s = load_settings(args.config)
    token = acquire_token(s)

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    # Este token não tem roles/scp: a autorização vem do caminho FMI.
    print(f"token: aud={claims.get('aud')} roles={claims.get('roles')} scp={claims.get('scp')}")

    url = (f"{args.host.rstrip('/')}/observabilityService/tenants/{s['tenant']}"
           f"/otlp/agents/{s['instance']}/traces?api-version=1")
    request = urllib.request.Request(
        url, data=json.dumps(build_span(s)).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode()
            print(f"HTTP {response.status}")
            print(body)
            return 0 if '"rejectedSpans":0' in body.replace(" ", "") else 1
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}")
        print("body:", exc.read().decode(errors="replace")[:500])
        print("www-authenticate:", exc.headers.get("WWW-Authenticate"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
