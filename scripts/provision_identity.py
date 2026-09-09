#!/usr/bin/env python3
"""Provisiona blueprint, Entra Agent ID e Agent User via Microsoft Graph — headless.

Existe porque o `a365` CLI abre um navegador para autenticação interativa e não tem
modo não-interativo. As permissões, porém, existem como APPLICATION roles no Graph:
o browser é limitação da ferramenta, não da plataforma. Este script fala Graph direto
com client credentials, então roda num runner sem humano.

Rotas (type casts OData, extraídas do CLI e confirmadas com token app-only):

    blueprint      POST/GET /beta/applications          @odata.type agentIdentityBlueprint
    agent identity POST/GET /beta/servicePrincipals     @odata.type agentIdentity
    agent user     POST/GET /beta/users                 @odata.type agentUser

App roles necessárias no SP do pipeline (Microsoft Graph):
    AgentIdentityBlueprint.ReadWrite.All  7fddd33b-d884-4ec0-8696-72cff90ff825
    AgentIdentityBlueprintPrincipal.Create
    AgentIdentity.Create.All              ad25cc1d-84d8-47df-a08e-b34c2e800819
    AgentIdentity.ReadWrite.All           dcf7150a-88d4-4fe6-9be1-c2744c455397
    Application.ReadWrite.OwnedBy         18a4783c-866b-4cc7-a460-3d5e5662c884
    User.ReadWrite.All                    741f803b-c850-494e-b5df-cde7c675a1ca
    Directory.Read.All                    7ab1d382-f21e-4acd-a863-ba3e13f7da61

Idempotente: consulta antes de criar, em todos os passos. Rodar duas vezes não duplica.

Uso:
    python scripts/provision_identity.py build/<slug>/agent/agent_manifest.json \\
        --tenant-domain contoso.onmicrosoft.com --secret-out /caminho/seguro.json

Token: GRAPH_TOKEN, ou o contexto do Azure CLI (em CI, o azure/login com o SP).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.microsoft.com"
BETA = f"{GRAPH}/beta"

# Escopos que o agente recebe por padrão. Deliberadamente MENOR que o default do CLI,
# que concede Mail.ReadWrite, Files.ReadWrite.All e ChannelMessage.Send a qualquer
# agente. Least-privilege é escolha, não acidente — aumente por solicitação, não por
# conveniência.
DEFAULT_RESOURCE_SCOPES = {
    "9b975845-388f-4429-889e-eab1ef63949c": ["Agent365.Observability.OtelWrite"],
}


def graph_token() -> str:
    from_env = os.environ.get("GRAPH_TOKEN")
    if from_env:
        return from_env.strip()
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource-type", "ms-graph",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"Falha ao obter token do Graph: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


class Graph:
    def __init__(self, token: str, dry_run: bool = False):
        self._token = token
        self.dry_run = dry_run

    def call(self, method: str, url: str, body: dict | None = None) -> tuple[int, dict | str]:
        if self.dry_run and method != "GET":
            print(f"    [dry-run] {method} {url}")
            if body:
                print("    [dry-run] body:", json.dumps(body, ensure_ascii=False)[:400])
            # Devolve 201 com ids sintéticos para o fluxo seguir e mostrar todos os passos.
            return 201, {"id": "00000000-dry-run", "appId": "00000000-dry-run"}
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}"}
        if data:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read().decode()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw

    def get_one(self, url: str) -> dict | None:
        status, body = self.call("GET", url)
        if status != 200 or not isinstance(body, dict):
            return None
        values = body.get("value")
        if values:
            return values[0]
        return body if body.get("id") else None


def odata_filter(base: str, expression: str, extra: str = "") -> str:
    """$filter precisa ser URL-encoded — espaço cru quebra o urllib com InvalidURL."""
    query = "$filter=" + urllib.parse.quote(expression, safe="'")
    return f"{base}?{query}{extra}"


def fail(step: str, status: int, body) -> None:
    message = body
    if isinstance(body, dict):
        message = body.get("error", {}).get("message", json.dumps(body))
    sys.stdout.flush()  # sem isto o erro em stderr aparece antes do progresso e engana o diagnóstico
    raise SystemExit(f"\n{step} falhou (HTTP {status}): {str(message)[:500]}")


def post_with_retry(g: "Graph", step: str, url: str, body: dict,
                    attempts: int = 10, delay: int = 6) -> dict:
    """Objeto de diretório recém-criado demora a ser aceito como dependência de outra escrita.

    Sondar por leitura não resolve: o blueprint já estava legível quando a criação do seu
    principal devolveu 403. Por isso a nova tentativa é na própria escrita.
    """
    status: int = 0
    created: dict | str = {}
    for attempt in range(attempts):
        status, created = g.call("POST", url, body)
        if status in (200, 201):
            return created if isinstance(created, dict) else {}
        if status not in (403, 404):
            fail(step, status, created)
        if attempt + 1 < attempts:
            print(f"    {step}: dependência ainda não visível, nova tentativa ({attempt + 1}/{attempts})...")
            sys.stdout.flush()
            time.sleep(delay)
    fail(step, status, created)
    return {}


def wait_for(describe: str, probe, attempts: int = 12, delay: int = 5):
    """Objetos de diretório demoram a propagar; criar e usar em seguida falha sem isto."""
    for i in range(attempts):
        found = probe()
        if found:
            return found
        print(f"    aguardando {describe} propagar ({i + 1}/{attempts})...")
        time.sleep(delay)
    return None


def resolve_user(g: Graph, email: str) -> str | None:
    user = g.get_one(f"{BETA}/users/{urllib.parse.quote(email)}?$select=id")
    return user.get("id") if user else None


def ensure_blueprint(g: Graph, name: str, description: str, sponsor_id: str | None) -> dict:
    existing = g.get_one(odata_filter(
        f"{BETA}/applications/microsoft.graph.agentIdentityBlueprint",
        f"displayName eq '{name}'", "&$select=id,appId,displayName"))
    if existing:
        print(f"  blueprint '{name}' já existe — appId {existing['appId']}")
        return existing

    body = {
        "@odata.type": "#microsoft.graph.agentIdentityBlueprint",
        "displayName": name,
        "description": description[:1000],
        "signInAudience": "AzureADMyOrg",
    }
    if sponsor_id:
        body["sponsors@odata.bind"] = [f"{GRAPH}/v1.0/users/{sponsor_id}"]
        body["owners@odata.bind"] = [f"{GRAPH}/v1.0/users/{sponsor_id}"]

    status, created = g.call("POST", f"{BETA}/applications", body)
    # O próprio CLI faz esse degrau: alguns tenants recusam sponsors/owners no create.
    if status == 400 and sponsor_id:
        print("    400 com sponsors — repetindo sem sponsors")
        body.pop("sponsors@odata.bind", None)
        status, created = g.call("POST", f"{BETA}/applications", body)
        if status == 400:
            print("    400 com owners — repetindo sem owners")
            body.pop("owners@odata.bind", None)
            status, created = g.call("POST", f"{BETA}/applications", body)
    if status not in (200, 201):
        fail("Criação do blueprint", status, created)
    print(f"  blueprint criado — appId {created.get('appId')}")
    return created


def ensure_service_principal(g: Graph, app_id: str) -> dict:
    """O principal do blueprint NÃO é um servicePrincipal comum.

    Criar sem o type cast faz o passo seguinte falhar com 403 "The Agent Blueprint
    Principal for the Agent Blueprint does not exist" — o objeto existe, mas com o
    tipo errado.
    """
    existing = g.get_one(odata_filter(f"{GRAPH}/v1.0/servicePrincipals",
                                      f"appId eq '{app_id}'", "&$select=id,appId"))
    if existing:
        print(f"  principal do blueprint já existe — {existing['id']}")
        return existing
    body = {"@odata.type": "#microsoft.graph.agentIdentityBlueprintPrincipal", "appId": app_id}
    created = post_with_retry(g, "Criação do principal do blueprint",
                              f"{BETA}/servicePrincipals", body)
    print(f"  principal do blueprint criado — {created.get('id')}")
    return created


def add_secret(g: Graph, app_object_id: str, label: str) -> str | None:
    """Credencial do blueprint exige a rota com type cast e AgentIdentityBlueprint.AddRemoveCreds.All.

    Em `/beta/applications/{id}/addPassword` (sem o cast) o Graph devolve 404, como se o
    objeto não existisse.
    """
    url = (f"{BETA}/applications/microsoft.graph.agentIdentityBlueprint/"
           f"{app_object_id}/addPassword")
    status, body = g.call("POST", url, {"passwordCredential": {"displayName": label}})
    if status not in (200, 201):
        fail("Criação do segredo do blueprint", status, body)
    return body.get("secretText") if isinstance(body, dict) else None


def ensure_agent_identity(g: Graph, blueprint_app_id: str, display_name: str,
                          sponsor_id: str | None) -> dict:
    existing = g.get_one(odata_filter(
        f"{BETA}/servicePrincipals/microsoft.graph.agentIdentity",
        f"agentIdentityBlueprintId eq '{blueprint_app_id}'"))
    if existing:
        print(f"  agent identity já existe — {existing['id']}")
        return existing

    body = {
        "@odata.type": "#microsoft.graph.agentIdentity",
        "displayName": display_name,
        "agentIdentityBlueprintId": blueprint_app_id,
    }
    # "A sponsor is required to create an agent identity" — o sponsor é uma PESSOA,
    # e é o que dá dono humano ao agente. Sem ele o agente nasce órfão.
    if sponsor_id:
        body["sponsors@odata.bind"] = [f"{GRAPH}/v1.0/users/{sponsor_id}"]

    created = post_with_retry(g, "Criação do agent identity",
                              f"{BETA}/servicePrincipals", body)
    print(f"  agent identity criado — {created.get('id')}")
    return created


def ensure_agent_user(g: Graph, identity_id: str, upn: str, display_name: str,
                      usage_location: str) -> dict:
    """O agent user aponta para a identidade por `identityParent`, um TIPO COMPLEXO.

    Não é navegação: `identityParent@odata.bind` é recusado. E `agentIdentityBlueprintId`
    é read-only — o servidor deriva a partir do parent. Sem o parent, o Graph devolve
    "The identity parent is required when creating an agent user".
    """
    existing = g.get_one(odata_filter(f"{BETA}/users/microsoft.graph.agentUser",
                                      f"userPrincipalName eq '{upn}'"))
    if existing:
        print(f"  agent user já existe — {existing['id']}")
        return existing
    body = {
        "@odata.type": "#microsoft.graph.agentUser",
        "displayName": display_name,
        "userPrincipalName": upn,
        "mailNickname": upn.split("@")[0],
        "accountEnabled": True,
        "usageLocation": usage_location,
        "identityParentType": "agentIdentity",
        "identityParent": {"id": identity_id},
    }
    created = post_with_retry(g, "Criação do agent user", f"{BETA}/users", body)
    print(f"  agent user criado — {created.get('id')}")
    return created


def assign_manager(g: Graph, user_id: str, manager_id: str) -> None:
    status, body = g.call("PUT", f"{BETA}/users/{user_id}/manager/$ref",
                          {"@odata.id": f"{GRAPH}/v1.0/users/{manager_id}"})
    if status in (200, 204):
        print("  manager atribuído")
    else:
        print(f"  AVISO: não consegui atribuir manager (HTTP {status})")


def grant_scopes(g: Graph, client_sp_id: str, resource_app_id: str, scopes: list[str]) -> None:
    resource = g.get_one(odata_filter(f"{GRAPH}/v1.0/servicePrincipals",
                                      f"appId eq '{resource_app_id}'", "&$select=id"))
    if not resource:
        print(f"  AVISO: resource {resource_app_id} não encontrado no tenant")
        return
    existing = g.get_one(odata_filter(
        f"{GRAPH}/v1.0/oauth2PermissionGrants",
        f"clientId eq '{client_sp_id}' and resourceId eq '{resource['id']}'"))
    if existing:
        print(f"  grant para {resource_app_id} já existe")
        return
    status, body = g.call("POST", f"{GRAPH}/v1.0/oauth2PermissionGrants", {
        "clientId": client_sp_id,
        "consentType": "AllPrincipals",
        "resourceId": resource["id"],
        "scope": " ".join(scopes),
    })
    if status in (200, 201):
        print(f"  grant criado para {resource_app_id}: {' '.join(scopes)}")
    else:
        print(f"  AVISO: grant para {resource_app_id} falhou (HTTP {status})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", type=pathlib.Path)
    ap.add_argument("--tenant-domain", default=os.getenv("A365_TENANT_DOMAIN", ""),
                    help="ex: contoso.onmicrosoft.com — compõe o UPN do agent user")
    ap.add_argument("--secret-out", type=pathlib.Path,
                    help="Arquivo (modo 0600) onde gravar o segredo do blueprint")
    ap.add_argument("--skip-agent-user", action="store_true",
                    help="Só blueprint + identidade. Agent user consome licença.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    slug = manifest["agentName"]
    # O a365 exige nome só com letras e números — o slug do formulário tem hífens.
    a365_name = re.sub(r"[^a-z0-9]", "", slug.lower()) or "agent"
    if not a365_name[0].isalpha():
        a365_name = "agent" + a365_name

    g = Graph(graph_token(), dry_run=args.dry_run)

    print(f"Provisionando '{slug}' (nome a365: {a365_name})")
    sponsor_id = None
    owner_email = manifest.get("businessOwner")
    if owner_email:
        sponsor_id = resolve_user(g, owner_email)
        print(f"  sponsor: {owner_email} -> {sponsor_id or 'NÃO ENCONTRADO'}")
    if not sponsor_id:
        print("  AVISO: sem sponsor. O agente nasce sem dono humano no diretório.")

    blueprint = ensure_blueprint(g, f"{a365_name} Blueprint",
                                 manifest.get("purpose", ""), sponsor_id)
    blueprint_app_id = blueprint.get("appId")
    blueprint_object_id = blueprint.get("id")

    if not args.dry_run:
        wait_for("blueprint", lambda: g.get_one(odata_filter(
            f"{GRAPH}/v1.0/applications", f"appId eq '{blueprint_app_id}'", "&$select=id")))

    ensure_service_principal(g, blueprint_app_id)

    secret = None
    if not args.dry_run and args.secret_out:
        secret = add_secret(g, blueprint_object_id, f"agent-factory-{slug}")

    identity = ensure_agent_identity(g, blueprint_app_id, f"{a365_name} Identity", sponsor_id)
    identity_id = identity.get("id")

    if not args.dry_run and identity_id:
        wait_for("agent identity", lambda: g.get_one(
            f"{GRAPH}/v1.0/servicePrincipals/{identity_id}?$select=id"))

    agent_user_id = None
    if not args.skip_agent_user and args.tenant_domain and identity_id:
        user = ensure_agent_user(g, identity_id, f"{a365_name}@{args.tenant_domain}",
                                 f"{manifest.get('displayName', slug)} Agent User", "US")
        agent_user_id = user.get("id")
        if agent_user_id and sponsor_id and not args.dry_run:
            assign_manager(g, agent_user_id, sponsor_id)

    if identity_id and not args.dry_run:
        print("  concedendo escopos ao agent identity")
        for resource_app_id, scopes in DEFAULT_RESOURCE_SCOPES.items():
            grant_scopes(g, identity_id, resource_app_id, scopes)

    result = {
        "agentName": slug,
        "a365Name": a365_name,
        "blueprintAppId": blueprint_app_id,
        "blueprintObjectId": blueprint_object_id,
        "agentIdentityId": identity_id,
        "agentUserId": agent_user_id,
        "sponsorUserId": sponsor_id,
    }
    print("\nResultado:")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if secret and args.secret_out:
        args.secret_out.parent.mkdir(parents=True, exist_ok=True)
        args.secret_out.write_text(json.dumps({**result, "blueprintClientSecret": secret},
                                              indent=2), encoding="utf-8")
        args.secret_out.chmod(0o600)
        # Nunca no stdout: o a365 CLI imprime o segredo em claro, e é um erro a não repetir.
        print(f"\nSegredo do blueprint gravado em {args.secret_out} (modo 0600).")
        print("Mova para o Key Vault e apague o arquivo.")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"blueprint_app_id={blueprint_app_id}\n")
            fh.write(f"agent_identity_id={identity_id}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
