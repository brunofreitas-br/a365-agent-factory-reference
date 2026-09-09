#!/usr/bin/env python3
"""Estágio 07 do waterfall: encontra agentes cuja data de revisão venceu.

Toda solicitação declara uma `review_date`. Sem alguém olhando essa data, um agente
provisionado vive para sempre — que é como nascem os agentes órfãos que ninguém
sabe explicar. Este script transforma a data em trabalho visível.

Uso:
    python scripts/agentes_para_revisar.py [--dias-de-antecedencia 7] [--out vencidos.json]
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
REQUESTS = ROOT / "requests"


def carregar(caminho: pathlib.Path) -> dict | None:
    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(f"AVISO: {caminho.name} não é YAML válido ({exc}); ignorado.", file=sys.stderr)
        return None
    return dados if isinstance(dados, dict) else None


def data_de_revisao(req: dict) -> datetime.date | None:
    bruto = req.get("review_date")
    if isinstance(bruto, datetime.date):
        return bruto
    try:
        return datetime.date.fromisoformat(str(bruto).strip())
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dias-de-antecedencia", type=int, default=7,
                        help="avisa este tanto de dias antes de vencer")
    parser.add_argument("--out", default="vencidos.json")
    args = parser.parse_args()

    hoje = datetime.date.today()
    limite = hoje + datetime.timedelta(days=args.dias_de_antecedencia)
    vencidos = []

    for caminho in sorted(REQUESTS.glob("*.yaml")):
        req = carregar(caminho)
        if not req:
            continue
        revisao = data_de_revisao(req)
        if revisao is None:
            # Sem data legível não dá para revisar — e silenciar isso recria o problema.
            print(f"AVISO: {caminho.name} sem review_date utilizável.", file=sys.stderr)
            continue
        if revisao > limite:
            continue
        vencidos.append({
            "arquivo": caminho.name,
            "agent_name": req.get("agent_name", caminho.stem),
            "display_name": req.get("display_name", ""),
            "purpose": req.get("purpose", ""),
            "business_owner": req.get("business_owner", ""),
            "technical_owner": req.get("technical_owner", ""),
            "environment": req.get("environment", ""),
            "data_sensitivity": req.get("data_sensitivity", ""),
            "writes": bool(req.get("writes")),
            "autonomous": bool(req.get("autonomous")),
            "review_date": revisao.isoformat(),
            "dias_de_atraso": (hoje - revisao).days,
        })

    pathlib.Path(args.out).write_text(
        json.dumps(vencidos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(vencidos)} agente(s) para revisar até {limite.isoformat()}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
