#!/usr/bin/env python3
"""Converte o payload do repository_dispatch em requests/<slug>.yaml.

O payload vem de um formulário aberto a makers — ou seja, é entrada não confiável.
Duas defesas deliberadas:

  1. O payload é lido de $GITHUB_EVENT_PATH em Python. Nunca é interpolado em shell
     nem concatenado em YAML — evita injeção de comando e de documento.
  2. Só as chaves conhecidas são copiadas, com coerção de tipo e normalização do slug.
     Campo extra no payload é descartado em silêncio, não gravado.

Exporta slug, display_name e requester para $GITHUB_OUTPUT.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

TEXT_FIELDS = ("display_name", "purpose", "business_owner", "technical_owner")
ENUM_FIELDS = {
    "agent_kind": {"agent", "ai-teammate"},
    "auth_mode": {"s2s", "obo"},
    "environment": {"dev", "prod"},
    "data_sensitivity": {"publico", "interno", "confidencial", "restrito"},
}
BOOL_FIELDS = ("writes", "autonomous")
VALID_WORKIQ = {"mail", "calendar", "teams", "sharepoint", "onedrive",
                "word", "user", "copilot", "dataverse"}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:40].rstrip("-")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,38}[a-z0-9]", slug):
        raise SystemExit(
            f"Não consegui derivar um nome válido de agente a partir de '{value}'. "
            "Use 4 a 40 caracteres, começando por letra."
        )
    return slug


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "sim", "yes", "1", "verdadeiro"}


def as_list(value) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[;,\n]", str(value or ""))
    return [item.strip() for item in items if item and item.strip()]


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        print("GITHUB_EVENT_PATH ausente — este script roda dentro do Actions.", file=sys.stderr)
        return 1

    event = json.loads(pathlib.Path(event_path).read_text(encoding="utf-8"))
    payload = event.get("client_payload") or {}
    # A API de dispatch rejeita client_payload com mais de 10 chaves de primeiro nível,
    # então os campos vêm aninhados sob "request".
    if isinstance(payload.get("request"), dict):
        payload = payload["request"]
    if not payload:
        print("client_payload vazio.", file=sys.stderr)
        return 1

    slug = slugify(payload.get("agent_name") or payload.get("display_name") or "")

    request: dict = {"agent_name": slug}
    for field in TEXT_FIELDS:
        request[field] = " ".join(str(payload.get(field, "")).split())

    for field, allowed in ENUM_FIELDS.items():
        value = str(payload.get(field, "")).strip().lower()
        # Valor fora do domínio não é "corrigido" para um default permissivo:
        # vira o valor mais restritivo, e o Gate 1 reclama se estiver errado.
        if value not in allowed:
            value = {"agent_kind": "agent", "auth_mode": "s2s",
                     "environment": "dev", "data_sensitivity": "restrito"}[field]
        request[field] = value

    for field in BOOL_FIELDS:
        request[field] = as_bool(payload.get(field))

    request["audience"] = as_list(payload.get("audience"))
    request["workiq_tools"] = [t for t in as_list(payload.get("workiq_tools"))
                               if t.lower() in VALID_WORKIQ]
    request["review_date"] = str(payload.get("review_date", "")).strip()

    target = ROOT / "requests" / f"{slug}.yaml"
    if target.exists():
        raise SystemExit(
            f"Já existe uma solicitação para '{slug}'. Renomeie o agente ou edite o "
            "arquivo existente por PR."
        )

    header = (
        "# Gerado automaticamente pelo intake (Forms → Power Automate → repository_dispatch).\n"
        "# Editar por PR; não editar direto na main.\n"
    )
    target.write_text(
        header + yaml.safe_dump(request, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"Gerado {target.relative_to(ROOT)}")
    print(yaml.safe_dump(request, allow_unicode=True, sort_keys=False))

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        requester = str(payload.get("business_owner") or "desconhecido")
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"slug={slug}\n")
            fh.write(f"display_name={request['display_name']}\n")
            fh.write(f"requester={requester}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
