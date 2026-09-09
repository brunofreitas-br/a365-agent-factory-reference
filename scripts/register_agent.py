#!/usr/bin/env python3
"""Registra o agente no inventário do Microsoft 365 Copilot (Agent Registration API).

    POST https://graph.microsoft.com/beta/copilot/agentRegistrations
    Permissão: AgentRegistration.ReadWrite.All (aplicação — roda headless)

O que este script FAZ: coloca o agente no registro administrativo do M365, amarrado à
identidade do Entra (agentIdentityId), ao blueprint, aos donos e a um agent card.
Resolve a prova "registro" do modelo de governança.

O que ele NÃO faz: publicar o agente em catálogo ou definir quem pode utilizá-lo. Nesta
implementação, o registro ocorre somente depois dos Gates de política, aprovação humana e
validação pré-deploy.

Comportamento verificado no tenant (set/2026, POST/PATCH/GET reais):
  - O `id` do registro **é o `sourceAgentId`**, não um GUID. Mandamos o slug do agente,
    e o registro passa a ser endereçável por ele.
  - POST repetido com o mesmo `sourceAgentId` devolve 201 e o mesmo id: a operação é um
    **upsert**, não duplica. Rodar o pipeline duas vezes é seguro.
  - `agentCard.provider` tem que ser OBJETO (`{organization, url}`). A documentação mostra
    uma string no exemplo e isso devolve 500 com
    "could not be converted to ... AgentProviderRequest".
  - Não existe GET de coleção: `GET /agentRegistrations` devolve 404. Só dá para consultar
    por id — ou seja, não há como listar o inventário por esta API.
  - `ownerIds` é preenchido com `createdBy` quando não é enviado — mas se esse valor for um
    service principal, a coluna **Owner do Admin Center fica vazia**. Owner precisa ser
    object ID de USUÁRIO. O painel "Agents without owners" é uma métrica de governança:
    registrar sem dono estraga o próprio inventário que se quer construir.
  - Sem `agentIdentityId`, o registro aparece no Admin Center com "Entra agent ID: —".
    Um registro sem identidade é uma linha numa lista, não um agente governável.

Limitações que permanecem:
  - API em /beta. A própria documentação diz que não é suportada em produção.
  - `AgentRegistration.ReadWrite.All` é escrita em TODO o tenant. O service principal do
    pipeline vira alvo de valor alto — use identidade dedicada, federated credential
    (sem segredo) e nada além desta permissão.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

GRAPH_ENDPOINT = "https://graph.microsoft.com/beta/copilot/agentRegistrations"


def graph_token() -> str:
    """Token do Graph via contexto do Azure CLI (em CI, herdado do azure/login OIDC).

    Em CI o `azure/login` autentica como o service principal do pipeline, então o token
    já carrega o app role. Localmente o token do az CLI não tem o escopo — por isso o
    override por GRAPH_TOKEN.
    """
    from_env = os.environ.get("GRAPH_TOKEN")
    if from_env:
        return from_env.strip()

    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource-type", "ms-graph",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"Falha ao obter token do Graph: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def build_agent_card(manifest: dict, agent_url: str) -> dict:
    return {
        "name": manifest["displayName"],
        "version": "1.0.0",
        "description": manifest["purpose"],
        "url": agent_url,
        # provider PRECISA ser objeto — string devolve 500 (AgentProviderRequest).
        "provider": {
            "organization": manifest.get("businessOwner", "").split("@")[-1] or "interno",
            "url": os.getenv("AGENT_FACTORY_REPO_URL", "https://example.invalid"),
        },
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": manifest["agentName"],
                "name": manifest["displayName"],
                "description": manifest["purpose"],
            }
        ],
    }


def graph_get(url: str, token: str) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def build_payload(manifest: dict, created_by: str, owner_ids: list[str],
                  managed_by_app_id: str | None, agent_url: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    payload = {
        "displayName": manifest["displayName"],
        "description": manifest["purpose"],
        "createdBy": created_by,
        "sourceCreatedDateTime": now,
        "sourceLastModifiedDateTime": now,
        # Vira o `id` do registro — é o que torna a operação um upsert.
        "sourceAgentId": manifest["agentName"],
        "originatingStore": "AgentFactory",
        "agentCard": build_agent_card(manifest, agent_url),
    }
    # A doc exige owners OU managedByAppId. Mandamos o que houver — sem os dois,
    # o registro fica órfão e ninguém responde por ele.
    if owner_ids:
        payload["ownerIds"] = owner_ids
    if managed_by_app_id:
        payload["managedByAppId"] = managed_by_app_id

    instance_id = os.getenv("A365_AGENT_INSTANCE_ID")
    blueprint_id = os.getenv("A365_BLUEPRINT_CLIENT_ID")
    if instance_id:
        payload["agentIdentityId"] = instance_id
    if blueprint_id:
        payload["agentIdentityBlueprintId"] = blueprint_id
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", type=pathlib.Path,
                    help="build/<slug>/agent/agent_manifest.json")
    ap.add_argument("--created-by", required=True,
                    help="Object ID do usuário ou app que criou o registro")
    ap.add_argument("--owner-ids", default="",
                    help="Object IDs de USUÁRIO dos donos, separados por vírgula. "
                         "Service principal aqui deixa a coluna Owner vazia no Admin Center.")
    ap.add_argument("--allow-orphan", action="store_true",
                    help="Permite registrar sem dono. Use só se for mesmo intencional.")
    ap.add_argument("--managed-by-app-id", default=os.getenv("A365_MANAGED_BY_APP_ID", ""))
    ap.add_argument("--agent-url", default=os.getenv("AGENT_ENDPOINT_URL", ""),
                    help="Endpoint HTTP do agente (FQDN do Container App + /invoke)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Mostra o payload e não chama o Graph")
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    owner_ids = [o.strip() for o in args.owner_ids.split(",") if o.strip()]
    agent_url = args.agent_url or f"https://{manifest['agentName']}.invalid/invoke"
    payload = build_payload(manifest, args.created_by, owner_ids,
                            args.managed_by_app_id or None, agent_url)

    print("Payload do agentRegistration:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    problemas = []
    if not owner_ids:
        problemas.append(
            "ownerIds vazio. O Admin Center conta 'Agents without owners' — registrar sem "
            "dono estraga o inventário. Passe --owner-ids com object IDs de USUÁRIO "
            "(service principal não preenche a coluna Owner).")
    if not payload.get("agentIdentityId"):
        problemas.append(
            "agentIdentityId ausente. O agente vai aparecer com 'Entra agent ID: —', ou seja, "
            "sem identidade — defina A365_AGENT_INSTANCE_ID depois de rodar "
            "`a365 create-instance identity`.")

    for p in problemas:
        print(f"\nAVISO: {p}")

    if args.dry_run:
        print("\n--dry-run: nada foi enviado ao Graph.")
        return 0

    if not owner_ids and not args.allow_orphan:
        raise SystemExit(
            "\nAbortado: registro sem dono. Use --allow-orphan se isso for mesmo intencional.")

    token = graph_token()

    # O id do registro é o sourceAgentId, então dá para saber de antemão se isto é
    # criação ou atualização — e dizer isso no log em vez de deixar ambíguo.
    slug = manifest["agentName"]
    existing, _ = graph_get(f"{GRAPH_ENDPOINT}/{slug}", token)
    print(f"\nRegistro '{slug}' {'já existe — atualizando' if existing == 200 else 'não existe — criando'}.")

    request = urllib.request.Request(
        GRAPH_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:600]
        raise SystemExit(f"Graph retornou {exc.code}: {detail}")

    registration_id = body.get("id", "")
    print(f"\nHTTP {status} — agentRegistration id: {registration_id}")

    out = os.environ.get("GITHUB_OUTPUT")
    if out and registration_id:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"registration_id={registration_id}\n")

    args.manifest.parent.joinpath("agent_registration.json").write_text(
        json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
