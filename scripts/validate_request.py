#!/usr/bin/env python3
"""Gate 1: valida uma solicitação de agente contra o schema e as regras de política.

Uso:
    python scripts/validate_request.py requests/meu-agente.yaml
    python scripts/validate_request.py --all

Sai com código 1 se houver erro. Avisos não bloqueiam.
Escreve um relatório markdown no stdout e, se disponível, em $GITHUB_STEP_SUMMARY.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "requests" / "schema.json"

# Combinações que exigem revisão explícita antes do merge.
HIGH_RISK_SENSITIVITY = {"confidencial", "restrito"}


class Finding:
    def __init__(self, level: str, message: str):
        self.level = level  # "erro" | "aviso"
        self.message = message

    def __str__(self) -> str:
        return f"{self.level.upper()}: {self.message}"


def check_policy(req: dict) -> list[Finding]:
    """Regras que o JSON Schema não consegue expressar."""
    out: list[Finding] = []

    auth_mode = req.get("auth_mode")
    workiq = req.get("workiq_tools") or []

    if workiq:
        out.append(Finding(
            "erro",
            "WorkIQ nao esta implementado neste runtime. O perfil OBO inicial oferece somente "
            "a ferramenta de leitura Graph /me; remova workiq_tools ou use um runtime compativel.",
        ))
    if auth_mode == "obo" and req.get("autonomous"):
        out.append(Finding(
            "erro",
            "OBO humano requer um access token de usuario em cada invocacao. "
            "Execucao autonoma com user_fic ou refresh token nao esta implementada.",
        ))

    # O lab só provisiona Agent (não-teammate). AI Teammate precisa de Agentic User + licença.
    if req.get("agent_kind") == "ai-teammate":
        out.append(Finding(
            "erro",
            "agent_kind 'ai-teammate' não é suportado por este pipeline. "
            "AI Teammate exige Agentic User com UPN, mailbox e licença — provisione pela skill "
            "'make-ai-teammate' com humano no loop.",
        ))

    # Data de revisão precisa ser futura e não pode ser eterna.
    raw_date = req.get("review_date")
    if raw_date:
        try:
            review = dt.date.fromisoformat(str(raw_date))
        except ValueError:
            out.append(Finding("erro", f"review_date '{raw_date}' não é uma data ISO válida."))
        else:
            today = dt.date.today()
            if review <= today:
                out.append(Finding("erro", f"review_date {review} já passou."))
            elif review > today + dt.timedelta(days=550):
                out.append(Finding(
                    "aviso",
                    f"review_date {review} está a mais de 18 meses. Revisão longa demais "
                    "costuma virar agente órfão.",
                ))

    # Dono de negócio e dono técnico iguais: ninguém revisa ninguém.
    if req.get("business_owner") and req.get("business_owner") == req.get("technical_owner"):
        out.append(Finding(
            "aviso",
            "business_owner e technical_owner são a mesma pessoa. Sem segregação, "
            "a revisão periódica perde o efeito.",
        ))

    if req.get("environment") == "prod" and req.get("data_sensitivity") == "restrito":
        out.append(Finding(
            "aviso",
            "Dado 'restrito' em produção: confirme que o escopo do blueprint é o mínimo "
            "necessário antes de aprovar.",
        ))

    return out


def classify_risk(req: dict) -> tuple[str, list[str]]:
    """Classificação simples e explicável. O rigor do resto do fluxo deriva daqui."""
    reasons: list[str] = []
    score = 0

    if req.get("data_sensitivity") in HIGH_RISK_SENSITIVITY:
        score += 2
        reasons.append(f"dado {req['data_sensitivity']}")
    if req.get("writes"):
        score += 1
        reasons.append("escreve em sistema")
    if req.get("autonomous"):
        score += 1
        reasons.append("age sem gatilho humano")
    if req.get("environment") == "prod":
        score += 1
        reasons.append("ambiente de produção")

    level = "alto" if score >= 4 else "médio" if score >= 2 else "baixo"
    return level, reasons


def validate_one(path: pathlib.Path, validator: Draft202012Validator) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    try:
        req = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [Finding("erro", f"YAML inválido: {exc}")], {}

    if not isinstance(req, dict):
        return [Finding("erro", "O arquivo precisa conter um mapa YAML no topo.")], {}

    for err in sorted(validator.iter_errors(req), key=lambda e: list(e.path)):
        loc = ".".join(str(p) for p in err.path) or "(raiz)"
        findings.append(Finding("erro", f"`{loc}`: {err.message}"))

    findings.extend(check_policy(req))

    # O nome do arquivo precisa bater com o agent_name para o pipeline achar o request.
    name = req.get("agent_name")
    if name and path.stem != name and not path.stem.startswith("exemplo-"):
        findings.append(Finding(
            "erro", f"O arquivo se chama '{path.stem}.yaml' mas agent_name é '{name}'. "
                    "Renomeie o arquivo para bater."))

    return findings, req


def render_report(results: list[tuple[pathlib.Path, list[Finding], dict]]) -> str:
    lines = ["# Validação de solicitação de agente", ""]
    for path, findings, req in results:
        errors = [f for f in findings if f.level == "erro"]
        warnings = [f for f in findings if f.level == "aviso"]
        icon = "FALHOU" if errors else "OK"
        lines.append(f"## `{path.name}` — {icon}")
        lines.append("")

        if req and not errors:
            level, reasons = classify_risk(req)
            because = ", ".join(reasons) if reasons else "nenhum fator agravante"
            lines.append(f"- **Agente:** {req.get('display_name')} (`{req.get('agent_name')}`)")
            lines.append(f"- **Modo:** {req.get('agent_kind')} / {req.get('auth_mode')} / "
                         f"{req.get('environment')}")
            lines.append(f"- **Risco: {level}** — {because}")
            lines.append(f"- **Donos:** {req.get('business_owner')} (negócio), "
                         f"{req.get('technical_owner')} (técnico)")
            if level == "alto":
                lines.append("")
                lines.append("> Risco alto: este PR precisa de aprovação explícita de segurança "
                             "além do CODEOWNERS.")
            lines.append("")

        if errors:
            lines.append("### Erros (bloqueiam o merge)")
            lines.extend(f"- {f.message}" for f in errors)
            lines.append("")
        if warnings:
            lines.append("### Avisos")
            lines.extend(f"- {f.message}" for f in warnings)
            lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", type=pathlib.Path)
    ap.add_argument("--all", action="store_true", help="Valida todos os requests/*.yaml")
    args = ap.parse_args()

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    paths = list(args.paths)
    if args.all or not paths:
        paths = sorted((ROOT / "requests").glob("*.yaml"))
    if not paths:
        print("Nenhuma solicitação para validar.")
        return 0

    results = []
    failed = False
    for path in paths:
        findings, req = validate_one(path, validator)
        results.append((path, findings, req))
        if any(f.level == "erro" for f in findings):
            failed = True

    report = render_report(results)
    print(report)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
