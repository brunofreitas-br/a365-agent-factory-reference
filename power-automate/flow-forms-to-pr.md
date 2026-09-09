# Flow: Forms → aprovação → repositório

Estágios 01 a 03 do waterfall. O flow faz três coisas: **normaliza** as respostas de negócio
em campos técnicos, **registra a decisão humana** (Gate 2) e **entrega o pedido ao
repositório**.

Ele não monta o YAML nem abre PR. Isso é deliberado — ver *Por que uma ação só*.

---

## Desenho

```
When a new response is submitted            (Microsoft Forms)
  └─ Get response details
       └─ Compose_slug, Compose_auth_mode, Compose_environment,
          Compose_sensitivity, Compose_writes, Compose_autonomous,
          Compose_audience, Compose_workiq
            └─ Compose_yaml                 (só para exibir no cartão de aprovação)
              └─ Start and wait for an approval
                 ├─ Reprovado → Send an email (V2) com o comentário do aprovador
                 └─ Aprovado  → Compose_payload
                                └─ Create a repository dispatch event   (GitHub)
```

Uma única ação de GitHub. Sem PAT, sem Key Vault, sem segredo no flow.

---

## Por que uma ação só

O desenho original tentava montar o arquivo e abrir o PR pelo conector nativo. **Não é
possível:** o conector do GitHub não tem ação para escrever arquivo. O que existe é
`Create or update a repository **secret**`, que é outra coisa — o nome truncado na interface
engana.

Ações de escrita que o conector realmente oferece: `Create a pull request`,
`Create a reference`, `Merge a pull request`, `Create an issue` e
`Create a repository dispatch event`.

Sem escrita de arquivo, o conteúdo precisa nascer do outro lado. Isso acabou sendo melhor:
a lógica de montagem do YAML vive em `scripts/intake_from_dispatch.py`, versionada,
revisável em PR e testável fora do Power Automate.

---

## Ação 1 — Trigger

**Microsoft Forms → When a new response is submitted.** Selecione o formulário descrito em
`forms/solicitacao-de-agente.md`.

## Ação 2 — Get response details

Mesmo formulário, `Response Id` do trigger.

## Ações 3 a 10 — Composes de normalização

O formulário pergunta em linguagem de negócio; o pipeline consome campos técnicos. Cada
`Compose` faz uma tradução. Substitua `<ID_PERGUNTA_N>` pelos identificadores do seu
formulário — eles aparecem no seletor de conteúdo dinâmico como `body/rXXXXXXXX`.

| Compose | Expressão (padrão) |
|---|---|
| `Compose_slug` | `toLower(replace(trim(outputs('Get_response_details')?['body/<ID_1>']),' ','-'))` |
| `Compose_sensitivity` | `toLower(first(split(outputs('Get_response_details')?['body/<ID_5>'],' ')))` |
| `Compose_writes` | `if(contains(outputs('Get_response_details')?['body/<ID_6>'],'escreve'),'true','false')` |
| `Compose_autonomous` | `if(contains(outputs('Get_response_details')?['body/<ID_7>'],'Sozinho'),'true','false')` |
| `Compose_auth_mode` | `if(startsWith(outputs('Get_response_details')?['body/<ID_8>'],'Não'),'s2s','obo')` |
| `Compose_workiq` | deriva de `<ID_8>`: `mail`, `calendar`, `sharepoint,onedrive` ou vazio |
| `Compose_audience` | `outputs('Get_response_details')?['body/<ID_9>']` (lista separada por vírgula) |
| `Compose_environment` | `if(contains(outputs('Get_response_details')?['body/<ID_10>'],'Produção'),'prod','dev')` |

**Não use `Compose` dentro de um `Switch`.** O resultado não é referenciável fora do case.
Se precisar de ramificação, use `Initialize variable` e `Set variable`, ou aninhe `if()`.

## Ação 11 — Compose_yaml

Texto legível da solicitação, só para o corpo do cartão de aprovação. O aprovador precisa
ver o que está aprovando.

## Ação 12 — Start and wait for an approval

Tipo **Approve/Reject – First to respond**.

Aprovadores derivados da sensibilidade do dado, com `if()` aninhado — a interface não aceita
expressão no tipo de aprovação, só no campo de destinatários:

```
if(equals(outputs('Compose_sensitivity'),'restrito'),
   concat(<dono_tecnico>,';',<dono_negocio>,';seguranca@contoso.com'),
   if(equals(outputs('Compose_sensitivity'),'confidencial'),
      concat(<dono_tecnico>,';',<dono_negocio>),
      <dono_tecnico>))
```

## Ação 13 — Condition

`outputs('Approvals')?['outcome']` é igual a `Approve`.

### Ramo *If no*

**Send an email (V2)** ao solicitante, incluindo
`outputs('Approvals')?['responses'][0]['comments']`. Sem isso a pessoa fica sem resposta e
conclui que o processo engoliu a solicitação.

### Ramo *If yes* — Compose_payload

Abra o **code view** do campo Inputs e cole o objeto abaixo. Autorar como objeto, e não como
texto concatenado, faz o runtime escapar aspas sozinho — se alguém escrever `"` no propósito,
o JSON não quebra.

```json
{
  "request": {
    "agent_name": "@{outputs('Compose_slug')}",
    "display_name": "@{outputs('Get_response_details')?['body/<ID_1>']}",
    "purpose": "@{outputs('Get_response_details')?['body/<ID_2>']}",
    "business_owner": "@{outputs('Get_response_details')?['body/<ID_3>']}",
    "technical_owner": "@{outputs('Get_response_details')?['body/<ID_4>']}",
    "agent_kind": "agent",
    "auth_mode": "@{outputs('Compose_auth_mode')}",
    "environment": "@{outputs('Compose_environment')}",
    "data_sensitivity": "@{outputs('Compose_sensitivity')}",
    "writes": "@{outputs('Compose_writes')}",
    "autonomous": "@{outputs('Compose_autonomous')}",
    "audience": "@{outputs('Compose_audience')}",
    "workiq_tools": "@{outputs('Compose_workiq')}",
    "review_date": "@{outputs('Get_response_details')?['body/<ID_11>']}"
  }
}
```

O aninhamento sob `request` não é estético. A API de dispatch do GitHub **recusa
`client_payload` com mais de 10 propriedades de primeiro nível** — são 14 campos, e sem o
aninhamento a chamada devolve `422`.

### Ramo *If yes* — Create a repository dispatch event

| Campo | Valor |
|---|---|
| Repository Owner | `<owner>` |
| Repository Name | `<repo>` |
| Event Name | `nova-solicitacao-agente` |
| Event Payload | `outputs('Compose_payload')` |

Opcionalmente, um **Send an email (V2)** de "recebemos, está sendo provisionado".

---

## O que o flow deliberadamente não faz

**Não acompanha o pipeline.** A ação de dispatch responde `204` sem corpo — não há número
de PR nem run id para capturar. Um laço de `Delay` com HTTP para acompanhar trocaria a
notificação nativa do GitHub por lógica frágil dentro do Power Automate.

**Não faz o merge.** O conector tem `Merge a pull request`, mas o `intake.yml` já mescla — e
só depois do Gate 1 passar. Mover isso para o flow tiraria a validação do caminho crítico.

---

## Registro da decisão

O histórico do Approvals registra quem aprovou e quando. Para requisitos formais de
auditoria, recomenda-se persistir no ramo aprovado uma gravação em lista do SharePoint ou
tabela do Dataverse com solicitante, aprovador, data e o YAML. Esse registro passa a ser a
trilha de evidência do Gate 2.
