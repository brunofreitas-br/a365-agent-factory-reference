"""Console de agentes: uma porta única, autenticada, para os agentes internos.

Os agentes rodam com ingress interno e escalam a zero. Sem uma porta de entrada não
há como exercitá-los — e expor cada um publicamente seria trocar governança por
conveniência. Este console é a porta.

Descoberta é por tag (`a365-managed-by=agent-factory`), não por lista fixa: agente
provisionado depois aparece aqui sem redeploy.
"""
from __future__ import annotations

import json
import logging
import os

import httpx
from azure.identity import DefaultAzureCredential
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("console")

SUBSCRIPTION = os.environ["AZURE_SUBSCRIPTION_ID"]
RESOURCE_GROUP = os.environ["AZURE_RESOURCE_GROUP"]
ARM = "https://management.azure.com"
API_VERSION = "2024-03-01"

app = FastAPI(title="Console de Agentes")
_credential = DefaultAzureCredential()


def _arm_token() -> str:
    return _credential.get_token(f"{ARM}/.default").token


async def _listar_agentes() -> list[dict]:
    url = (f"{ARM}/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
           f"/providers/Microsoft.App/containerApps?api-version={API_VERSION}")
    async with httpx.AsyncClient(timeout=30) as client:
        resposta = await client.get(url, headers={"Authorization": f"Bearer {_arm_token()}"})
    resposta.raise_for_status()

    agentes = []
    for recurso in resposta.json().get("value", []):
        tags = recurso.get("tags") or {}
        if tags.get("a365-managed-by") != "agent-factory":
            continue
        fqdn = (((recurso.get("properties") or {}).get("configuration") or {})
                .get("ingress") or {}).get("fqdn")
        if not fqdn:
            continue
        agentes.append({
            "nome": tags.get("a365-agent-name", recurso["name"]),
            "titulo": tags.get("a365-display-name", recurso["name"]),
            "dono_negocio": tags.get("a365-business-owner", ""),
            "dono_tecnico": tags.get("a365-technical-owner", ""),
            "sensibilidade": tags.get("a365-data-sensitivity", ""),
            "revisao": tags.get("a365-review-date", ""),
            "url": f"https://{fqdn}/invoke",
        })
    return sorted(agentes, key=lambda a: a["titulo"])


def _usuario(request: Request) -> str:
    # Cabeçalho posto pela autenticação embutida do Container Apps. Se vier vazio em
    # produção, é sinal de que a autenticação está desligada — não de usuário anônimo.
    return request.headers.get("x-ms-client-principal-name", "(sem autenticação)")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/api/agentes")
async def api_agentes() -> JSONResponse:
    return JSONResponse(await _listar_agentes())


@app.post("/api/invocar")
async def api_invocar(request: Request) -> JSONResponse:
    corpo = await request.json()
    nome = str(corpo.get("agente", "")).strip()
    texto = str(corpo.get("texto", "")).strip()
    if not nome or not texto:
        raise HTTPException(status_code=400, detail="Informe 'agente' e 'texto'.")

    # O cliente manda o NOME; o endereço vem da listagem do ARM. Aceitar URL do
    # cliente transformaria este console num proxy aberto para a rede interna.
    agentes = {a["nome"]: a for a in await _listar_agentes()}
    if nome not in agentes:
        raise HTTPException(status_code=404, detail=f"Agente '{nome}' não encontrado.")

    usuario = _usuario(request)
    log.info("invocacao: usuario=%s agente=%s", usuario, nome)
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            # O contrato do agente é {"input": ...} — ver InvokeRequest em template/agent/main.py.
            resposta = await client.post(agentes[nome]["url"], json={"input": texto})
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Agente não respondeu: {exc}") from exc

    conteudo = resposta.text
    try:
        conteudo = resposta.json()
    except json.JSONDecodeError:
        pass
    return JSONResponse({"agente": nome, "chamado_por": usuario,
                         "status": resposta.status_code, "resposta": conteudo})


PAGINA = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Console de Agentes</title>
<style>
 :root{
   color-scheme:dark;
   --fundo:#0A1422; --superficie:#101C2E; --superficie-alta:#152238;
   --hairline:#1E2A44; --hairline-forte:#2A3A5C;
   --titulo:#FFFFFF; --corpo:#C3CAD9; --nota:#7F90AE;
   --ciano:#4FB3E8; --ambar:#E8C15A;
 }
 *{box-sizing:border-box}
 html,body{margin:0;background:var(--fundo);color:var(--corpo)}
 body{font-family:"Segoe UI","Segoe UI Web (West European)",system-ui,-apple-system,sans-serif;
      font-size:15px;line-height:1.65;-webkit-font-smoothing:antialiased}
 .faixa{height:3px;background:linear-gradient(100deg,#4FB3E8 0%,#5C7CE0 48%,#A97CD6 100%)}
 .pagina{max-width:78rem;margin:0 auto;padding:3.5rem 3.5rem 5rem}
 .topo{display:flex;justify-content:space-between;align-items:flex-end;gap:3rem;
       border-bottom:1px solid var(--hairline);padding-bottom:2rem;margin-bottom:3rem}
 .eyebrow{font-weight:600;font-size:11px;letter-spacing:.16em;text-transform:uppercase;
          color:var(--ciano);margin-bottom:.7rem}
 h1{font-family:"Segoe UI Light","Segoe UI",system-ui,sans-serif;font-weight:200;
    font-size:2.75rem;line-height:1.1;margin:0 0 .7rem;color:var(--titulo);letter-spacing:-.01em}
 .sub{font-family:"Segoe UI Semilight","Segoe UI",system-ui,sans-serif;font-weight:300;
      color:var(--corpo);font-size:1.05rem;max-width:40rem;margin:0}
 .quem{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--nota);
       text-align:right;white-space:nowrap;padding-left:1.25rem;border-left:2px solid var(--ciano)}
 .quem strong{display:block;color:var(--titulo);font-weight:600;font-size:13px;
              letter-spacing:0;text-transform:none;margin-top:.3rem}
 h2{font-weight:600;font-size:11px;letter-spacing:.16em;text-transform:uppercase;
    color:var(--nota);margin:0 0 1.15rem}
 .secao{margin-bottom:3.25rem}
 .grade{display:grid;grid-template-columns:repeat(auto-fit,minmax(19rem,1fr));gap:1px;
        background:var(--hairline);border:1px solid var(--hairline)}
 .cartao{background:var(--superficie);padding:1.4rem 1.5rem;transition:background .15s}
 .cartao:hover{background:var(--superficie-alta)}
 .cartao .nome{font-weight:600;font-size:15px;color:var(--titulo);margin-bottom:.75rem}
 .cartao .meta{font-size:12.5px;color:var(--nota);line-height:1.85}
 .cartao .meta b{color:var(--corpo);font-weight:400}
 .chips{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.9rem}
 .chip{font-size:10px;letter-spacing:.1em;text-transform:uppercase;font-weight:600;
       border:1px solid var(--hairline-forte);color:var(--nota);padding:.22rem .55rem}
 .chip.forte{background:var(--ambar);border-color:var(--ambar);color:#1A1300}
 .chip.aceso{border-color:var(--ciano);color:var(--ciano)}
 .vazio{background:var(--superficie);padding:1.6rem;color:var(--nota);grid-column:1/-1}
 .colunas{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.05fr);gap:2.5rem;
          align-items:start}
 label{font-weight:600;font-size:11px;letter-spacing:.16em;text-transform:uppercase;
       color:var(--nota);display:block;margin-bottom:.55rem}
 .campo{margin-bottom:1.35rem}
 select,textarea{width:100%;font:inherit;color:var(--titulo);background:var(--superficie);
   border:1px solid var(--hairline-forte);border-radius:0;padding:.8rem .9rem}
 select:focus,textarea:focus{outline:0;border-color:var(--ciano);
   box-shadow:0 0 0 1px var(--ciano)}
 textarea{min-height:9.5rem;resize:vertical;line-height:1.6}
 textarea::placeholder{color:#5B6B87}
 button{font:inherit;font-weight:600;letter-spacing:.02em;background:var(--ciano);color:#04121F;
        border:0;border-radius:0;padding:.8rem 2rem;cursor:pointer}
 button:hover:not(:disabled){background:#6FC5F2}
 button:disabled{background:var(--hairline-forte);color:var(--nota);cursor:default}
 .painel{background:var(--superficie);border:1px solid var(--hairline);
         border-left:3px solid var(--ciano);padding:1.6rem 1.8rem;min-height:100%}
 .painel.repouso{border-left-color:var(--hairline-forte)}
 .painel .titulo{font-family:"Segoe UI Light","Segoe UI",sans-serif;font-weight:300;
                 font-size:1.7rem;color:var(--titulo);margin:.15rem 0 .5rem;line-height:1.2}
 .painel .linha{font-size:13.5px;color:var(--corpo);margin:.5rem 0 0}
 .painel .assinatura{font-size:11.5px;color:var(--nota);margin-top:1rem}
 details{margin-top:1.4rem;border-top:1px solid var(--hairline);padding-top:1rem}
 summary{cursor:pointer;font-size:11px;letter-spacing:.14em;text-transform:uppercase;
         font-weight:600;color:var(--nota);list-style:none}
 summary::-webkit-details-marker{display:none}
 summary:hover{color:var(--ciano)}
 pre{margin:.9rem 0 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
     color:#8FA6D8;white-space:pre-wrap;word-break:break-word;max-height:22rem;overflow:auto}
 .rodape{margin-top:4rem;padding-top:1.25rem;border-top:1px solid var(--hairline);
         font-size:11px;color:#5B6B87;max-width:46rem}
 @media (max-width:900px){.colunas{grid-template-columns:minmax(0,1fr)}}
 @media (max-width:640px){.pagina{padding:2.25rem 1.25rem 3rem}h1{font-size:2rem}
   .topo{flex-direction:column;align-items:flex-start;gap:1.5rem}
   .quem{text-align:left;border-left:0;padding-left:0;border-top:2px solid var(--ciano);padding-top:.6rem}}
</style></head><body>
<div class="faixa"></div>
<div class="pagina">

  <header class="topo">
    <div>
      <div class="eyebrow">Agent 365 &middot; Agent Factory</div>
      <h1>Console de Agentes</h1>
      <p class="sub">Porta única e autenticada para os agentes governados desta fábrica.
         O inventário vem das tags dos recursos, não de uma lista mantida à mão.</p>
    </div>
    <div class="quem">Autenticado como<strong id="quem">&mdash;</strong></div>
  </header>

  <section class="secao">
    <h2>Agentes provisionados</h2>
    <div class="grade" id="lista"></div>
  </section>

  <section class="colunas">
    <div>
      <h2>Invocar</h2>
      <div class="campo">
        <label for="agente">Agente</label>
        <select id="agente"></select>
      </div>
      <div class="campo">
        <label for="texto">Entrada</label>
        <textarea id="texto" placeholder="Descreva o alerta, o chamado ou o evento a classificar."></textarea>
      </div>
      <button id="enviar">Invocar agente</button>
    </div>

    <div>
      <h2>Resultado</h2>
      <div class="painel repouso" id="painel">
        <div class="eyebrow" id="p-eyebrow" style="color:var(--nota)">Aguardando</div>
        <p class="titulo" id="p-titulo">&mdash;</p>
        <div class="chips" id="p-chips"></div>
        <p class="linha" id="p-saida">A resposta do agente aparece aqui.</p>
        <p class="assinatura" id="p-quem"></p>
        <details id="p-detalhes" hidden>
          <summary>Resposta completa</summary>
          <pre id="p-json"></pre>
        </details>
      </div>
    </div>
  </section>

  <p class="rodape">Acesso restrito a usuários atribuídos no Microsoft Entra ID.
     Toda invocação é registrada com a identidade de quem chamou, e o console resolve
     o endereço do agente pelo inventário — nunca por URL enviada pelo navegador.</p>
</div>
<script>
const $ = (id) => document.getElementById(id);

async function carregar(){
  const ags = await (await fetch('/api/agentes')).json();
  $('lista').innerHTML = ags.length ? ags.map(a => `
    <div class="cartao">
      <div class="nome">${a.titulo}</div>
      <div class="meta">
        Dono de negócio &nbsp;<b>${a.dono_negocio || '—'}</b><br>
        Dono técnico &nbsp;<b>${a.dono_tecnico || '—'}</b><br>
        Revisão até &nbsp;<b>${a.revisao || '—'}</b>
      </div>
      <div class="chips">
        <span class="chip aceso">${a.sensibilidade || 'sem classificação'}</span>
      </div>
    </div>`).join('')
    : '<div class="vazio">Nenhum agente provisionado ainda.</div>';
  $('agente').innerHTML = ags.map(a => `<option value="${a.nome}">${a.titulo}</option>`).join('');
}

async function quem(){
  const d = await (await fetch('/api/eu')).json();
  $('quem').textContent = d.usuario;
}

function mostrar(d){
  const r = d.resposta || {};
  const urg = r.urgency || r.urgencia || '';
  const ok = d.status === 200;
  $('painel').classList.remove('repouso');
  $('p-eyebrow').textContent = ok ? 'Classificação' : 'Erro ' + d.status;
  $('p-eyebrow').style.color = ok ? 'var(--ciano)' : 'var(--ambar)';
  $('p-titulo').textContent = r.category || r.categoria || (ok ? '—' : 'Falha na invocação');
  $('p-chips').innerHTML = urg
    ? `<span class="chip ${urg === 'alta' ? 'forte' : ''}">urgência ${urg}</span>` : '';
  $('p-saida').textContent = r.output || '';
  $('p-quem').textContent = d.chamado_por ? 'Invocado por ' + d.chamado_por : '';
  $('p-json').textContent = JSON.stringify(d, null, 2);
  $('p-detalhes').hidden = false;
}

$('enviar').onclick = async () => {
  const b = $('enviar');
  b.disabled = true; b.textContent = 'Invocando...';
  $('painel').classList.add('repouso');
  $('p-eyebrow').textContent = 'Em execução';
  $('p-eyebrow').style.color = 'var(--nota)';
  $('p-titulo').textContent = 'Invocando o agente';
  $('p-chips').innerHTML = '';
  $('p-saida').textContent = 'O agente pode estar escalado a zero — a primeira chamada demora mais.';
  $('p-quem').textContent = ''; $('p-detalhes').hidden = true;
  try{
    const r = await fetch('/api/invocar', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agente: $('agente').value, texto: $('texto').value})});
    mostrar(await r.json());
  } finally {
    b.disabled = false; b.textContent = 'Invocar agente';
  }
};

carregar(); quem();
</script></body></html>"""


@app.get("/api/eu")
async def api_eu(request: Request) -> dict:
    return {"usuario": _usuario(request)}


@app.get("/", response_class=HTMLResponse)
async def raiz() -> HTMLResponse:
    return HTMLResponse(PAGINA)
