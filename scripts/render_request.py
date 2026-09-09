#!/usr/bin/env python3
"""Renderiza uma solicitação aprovada em artefatos concretos de provisionamento.

Uso:
    python scripts/render_request.py requests/meu-agente.yaml --out build/

Produz, em build/<agent_name>/:
    a365.config.json    config do a365 CLI (base + overrides da solicitação)
    agent/              cópia de template/agent com o manifesto do agente injetado
    Dockerfile
    infra.params.json   parâmetros do Bicep do Container App

Este script NÃO chama o a365 CLI nem o Azure. Ele só materializa arquivos, para que
o dry-run e o provisionamento sejam passos separados e auditáveis.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import re
import shutil
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "template"


def deep_merge(base: dict, override: dict) -> dict:
    """Merge recursivo. Valores do override vencem; dicts são fundidos."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def a365_agent_name(slug: str) -> str:
    """O a365 CLI exige nome comecando por letra e SO com letras e numeros.

    O slug do formulario tem hifens, entao nao serve direto: 'classificador-de-chamados'
    vira 'classificadorchamados'.
    """
    name = re.sub(r"[^a-z0-9]", "", slug.lower())
    if not name or not name[0].isalpha():
        name = "agent" + name
    return name[:40]


def resource_names(req: dict) -> dict:
    """Nomes determinísticos derivados do agent_name. Mesma solicitação, mesmos recursos."""
    slug = req["agent_name"]
    env = req["environment"]
    # Container App: minúsculas, hífens, até 32 chars.
    app = re.sub(r"[^a-z0-9-]", "-", f"ca-{slug}-{env}")[:32].rstrip("-")
    return {
        "containerApp": app,
        "resourceGroup": f"rg-a365-agents-{env}",
        "image": f"{slug}:{{tag}}",
        "a365Name": a365_agent_name(slug),
    }


def build_a365_config(req: dict, base: dict) -> dict:
    """Injeta os valores da solicitação no config base do a365 CLI.

    O schema veio de rodar `a365 config init` de verdade (v1.1.132-preview). As chaves
    de tenant/subscription/clientApp vem do ambiente porque sao do tenant, nao do agente.
    """
    names = resource_names(req)
    a365_name = names["a365Name"]
    domain = os.getenv("A365_TENANT_DOMAIN", "exemplo.onmicrosoft.com")
    endpoint = os.getenv(
        "AGENT_ENDPOINT_URL",
        f"https://{names['containerApp']}.azurecontainerapps.io/api/messages")
    overrides = {
        "tenantId": os.getenv("AZURE_TENANT_ID", ""),
        "subscriptionId": os.getenv("AZURE_SUBSCRIPTION_ID", ""),
        "clientAppId": os.getenv("A365_CLIENT_APP_ID", ""),
        "managerEmail": req["technical_owner"],
        "resourceGroup": names["resourceGroup"],
        "messagingEndpoint": endpoint,
        "needDeployment": False,
        "agentIdentityDisplayName": f"{a365_name} Identity",
        "agentBlueprintDisplayName": f"{a365_name} Blueprint",
        "agentUserPrincipalName": f"{a365_name}@{domain}",
        "agentUserDisplayName": f"{req['display_name']} Agent User",
        "agentDescription": " ".join(req["purpose"].split()),
        "deploymentProjectPath": "./agent",
    }
    return deep_merge(base, overrides)


def build_infra_params(req: dict) -> dict:
    names = resource_names(req)
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "containerAppName": {"value": names["containerApp"]},
            "agentName": {"value": req["agent_name"]},
            "displayName": {"value": req["display_name"]},
            "environmentTag": {"value": req["environment"]},
            "businessOwner": {"value": req["business_owner"]},
            "technicalOwner": {"value": req["technical_owner"]},
            "dataSensitivity": {"value": req["data_sensitivity"]},
            "reviewDate": {"value": str(req["review_date"])},
        },
    }


def build_manifest(req: dict) -> dict:
    """Manifesto lido pelo agente em runtime. É a solicitação, congelada."""
    return {
        "agentName": req["agent_name"],
        "displayName": req["display_name"],
        "purpose": " ".join(req["purpose"].split()),
        "authMode": req["auth_mode"],
        "environment": req["environment"],
        "dataSensitivity": req["data_sensitivity"],
        "writes": req["writes"],
        "autonomous": req["autonomous"],
        "businessOwner": req["business_owner"],
        "technicalOwner": req["technical_owner"],
        "audience": req["audience"],
        "reviewDate": str(req["review_date"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("request", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "build")
    args = ap.parse_args()

    req = yaml.safe_load(args.request.read_text(encoding="utf-8"))
    req.pop("$schema", None)
    req.setdefault("workiq_tools", [])

    base_path = TEMPLATE / "a365.config.base.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))

    out_dir = args.out / req["agent_name"]
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    shutil.copytree(TEMPLATE / "agent", out_dir / "agent")
    shutil.copy2(TEMPLATE / "Dockerfile", out_dir / "Dockerfile")
    shutil.copytree(TEMPLATE / "infra", out_dir / "infra")

    (out_dir / "agent" / "agent_manifest.json").write_text(
        json.dumps(build_manifest(req), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "a365.config.json").write_text(
        json.dumps(build_a365_config(req, base), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "infra.params.json").write_text(
        json.dumps(build_infra_params(req), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Renderizado em {out_dir}")
    for item in sorted(out_dir.rglob("*")):
        if item.is_file():
            print(f"  {item.relative_to(out_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
