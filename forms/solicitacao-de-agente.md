# Formulário de solicitação de agente

Microsoft Forms. É o Estágio 01 do waterfall — o ponto onde a solicitação vira artefato.

**Princípio de desenho:** o formulário pergunta o que o *maker* sabe responder, não o que o pipeline precisa. "O agente escreve em algum sistema?" é respondível; "qual o auth mode?" não é. O que der para derivar, o pipeline deriva.

---

## Campos

| # | Pergunta | Tipo | Obrigatório | Vai para |
|---|---|---|---|---|
| 1 | Qual o nome do agente? | Texto curto | Sim | `display_name` → slug vira `agent_name` |
| 2 | O que ele faz? Descreva em duas ou três frases. | Texto longo | Sim | `purpose` (mínimo 30 caracteres) |
| 3 | Quem é o dono de negócio? | Texto (e-mail) | Sim | `business_owner` |
| 4 | Quem é o dono técnico? | Texto (e-mail) | Sim | `technical_owner` |
| 5 | Que tipo de dado o agente vai tocar? | Escolha única | Sim | `data_sensitivity` |
| 6 | O agente vai **escrever** em algum sistema, ou só ler? | Escolha única | Sim | `writes` |
| 7 | O agente age sozinho, sem alguém pedir? | Escolha única | Sim | `autonomous` |
| 8 | Ele precisa acessar e-mail, calendário ou arquivos de um usuário? | Escolha única | Sim | deriva `auth_mode` e `workiq_tools` |
| 9 | Quem vai usar o agente? | Texto longo | Sim | `audience` (e-mails separados por vírgula) |
| 10 | Ambiente | Escolha única | Sim | `environment` |
| 11 | Até quando este agente deve existir sem nova revisão? | Data | Sim | `review_date` |

### Opções das escolhas

**5 — Tipo de dado**
- `publico` — Informação pública, já divulgada fora da empresa
- `interno` — Informação interna, sem dado pessoal ou financeiro
- `confidencial` — Dado pessoal, contratual ou financeiro
- `restrito` — Dado regulado, segredo de negócio ou credencial

**6 — Escrita**
- `Só lê` → `writes: false`
- `Lê e escreve` → `writes: true`

**7 — Autonomia**
- `Só quando alguém pede` → `autonomous: false`
- `Sozinho, por agendamento ou evento` → `autonomous: true`

**8 — Acesso a dados de usuário**
- `Não, ele trabalha com dados de sistema` → `auth_mode: s2s`, `workiq_tools: []`
- `Sim, e-mail` → `auth_mode: obo`, `workiq_tools: [mail]`
- `Sim, calendário` → `auth_mode: obo`, `workiq_tools: [calendar]`
- `Sim, arquivos (SharePoint/OneDrive)` → `auth_mode: obo`, `workiq_tools: [sharepoint, onedrive]`
- `Sim, e-mail e calendário` → `auth_mode: obo`, `workiq_tools: [mail, calendar]`

**10 — Ambiente**
- `Laboratório / prova de conceito` → `dev`
- `Produção` → `prod`

---

## Por que a pergunta 8 é a mais importante

Ela decide o `auth_mode`, e o `auth_mode` decide o que é possível daí para frente:

- **`s2s`** — o agente roda sozinho, com credencial própria. É o que o Container App do lab suporta. **WorkIQ não funciona:** ele exige token delegado de um usuário real.
- **`obo`** — o agente age em nome de quem chamou. Habilita WorkIQ, mas exige alguém logado no momento da execução, o que muda o runtime e o desenho de rede.

Um maker que marca "sim, e-mail" está pedindo um agente arquiteturalmente diferente do que marca "não". Por isso a pergunta é sobre a necessidade, não sobre o modo.

O `validate_request.py` recusa a combinação `s2s` + `workiq_tools`, então um mapeamento errado no flow falha no Gate 1 em vez de virar um agente quebrado.

---

## Configurações do Forms

- **Coletar o nome de quem responde:** ligado. Sem isso, não há a quem voltar.
- **Uma resposta por pessoa:** desligado — um maker pode pedir vários agentes.
- **Aceitar respostas:** ligado, com data de encerramento vazia.
- **Restringir a pessoas da organização:** ligado.

---

## O que o formulário deliberadamente **não** pergunta

- Nome de recurso do Azure, resource group, região — derivados do slug e do ambiente
- Imagem de container, porta, réplicas — o template decide
- Escopos de Entra — derivados do `auth_mode` e das ferramentas
- Nível de risco — **calculado**, não declarado. Maker não classifica o próprio risco.

O risco sai de `data_sensitivity` + `writes` + `autonomous` + `environment` em `scripts/validate_request.py`. É uma soma explicável, para que o maker entenda por que caiu em risco alto e o revisor consiga contestar o critério.
