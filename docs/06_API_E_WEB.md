# 06 — API e interface web

## 1. Princípio

Um processo, duas superfícies — desde a Fase 13, literalmente um processo e **uma porta**:
`python -m pesquisa_precos` sobe a app de `web/app.py`, que serve o HTML e monta os routers de
`api/routers/` sob `/api`. Os dois **consomem os mesmos `services/`**. Nenhuma página acessa o
banco diretamente.

A autenticação difere porque os clientes diferem: o HTML é protegido por sessão de cookie
(`web/auth.py`), as rotas `/api` por `X-API-Token` (`api/auth.py`).

**Não existe mais CLI** (ADR-020). Tudo que se opera, opera-se por aqui — inclusive rodando na
máquina do próprio usuário, em `localhost`.

Isso é o que preserva a saída: se um dia quiser um front separado, ele aponta para `/api/*` e os
templates são apagados. Custo de migração ≈ zero.

**Invariante:** o processo web nunca executa trabalho pesado. `POST /executar` grava intenção,
dispara um subprocesso e retorna imediatamente.

## 2. Stack

| Camada | Escolha | Motivo |
|---|---|---|
| HTTP | FastAPI | schemas Pydantic já existem nas etapas; docs automáticas |
| Templates | Jinja2 | sem build step |
| Interatividade | HTMX | progresso e log são exatamente o caso de uso dele |
| Estado local de UI | Alpine.js | modais e toggles, sem bundler |
| Streaming | SSE (`text/event-stream`) | log e progresso ao vivo; WebSocket é excessivo |
| Estáticos | servidos pelo próprio app | um deploy só |

Sem npm, sem bundler, sem CDN externo. CSS e JS ficam em `web/static/`.

## 3. API — rotas

### 3.1 Leitura

```
GET  /api/health                       → banco, disco, versão
GET  /api/providers/status             → provedores configurados no banco
     (a SONDAGEM ao vivo das capacidades é a tela `/provedores`)

GET  /api/runs                         → lista, mais recentes primeiro
GET  /api/runs/{id}                    → run + todas as etapas + estado do grafo
GET  /api/runs/{id}/etapas/{etapa}     → detalhe: progresso, métricas, params efetivos
GET  /api/runs/{id}/etapas/{etapa}/preview   → amostra do resultado (p/ o gate)
GET  /api/runs/{id}/etapas/{etapa}/estimativa → unidades, chamadas, custo, duração
GET  /api/runs/{id}/log?desde={id}&nivel=    → paginado
GET  /api/runs/{id}/log/stream               → SSE
GET  /api/runs/{id}/progresso/stream         → SSE (heartbeat de todas as etapas)
GET  /api/runs/{id}/erros?etapa=             → erro_item pendentes
GET  /api/runs/{id}/custo                    → por etapa, por capacidade, total

GET  /api/custo/resumo?de=&ate=              → dashboard: por run, por etapa, por modelo
GET  /api/exports                            → exports gerados
GET  /api/exports/{id}/download

GET  /api/catalogo/{tipo}/{codigo}           → item do catálogo + grupos resultantes
GET  /api/itens/{item_key}                   → rastreio completo (a consulta de auditoria)
GET  /api/documentos/{numero_controle_pncp}  → documento, itens, extração, estado
GET  /api/termos                             → termos ativos/inativos + códigos atendidos
```

### 3.2 Comando

```
POST /api/runs                          {rotulo, modo, config_versao_id, teto_custo_usd,
                                         limite_documentos}
POST /api/runs/{id}/etapas/{etapa}/executar   {acao: atualizar|retomar|refazer, params: {...}}
POST /api/runs/{id}/etapas/{etapa}/cancelar
POST /api/runs/{id}/etapas/{etapa}/aprovar    {params_override: {...}}
POST /api/runs/{id}/etapas/{etapa}/pular      {motivo}
POST /api/runs/{id}/abortar
POST /api/runs/{id}/executar-tudo             → enfileira as etapas na ordem (modo sequencial)

POST /api/termos/{id}/testar            → consulta o PNCP e devolve a contagem de resultados
PATCH /api/termos/{id}                  {ativo: bool}

POST /api/documentos/{ncp}/reprocessar  {estrategia: janela|completa|visao}

POST /api/config/versoes                {rotulo, valores: {...}, notas}  → cria versão nova
GET  /api/config/versoes/{id}/diff/{outro_id}

GET  /api/prompts                       → prompts + versões
POST /api/prompts/{nome}/versoes        {template, notas}
POST /api/prompts/{nome}/versoes/{v}/ativar

PATCH /api/provedores/{nome}            {base_url, modelo_padrao, ativo, ...}
POST /api/provedores/{nome}/testar
```

### 3.3 Regras de comportamento

- `POST .../executar` retorna **202** com o `run_etapa_id`. Nunca bloqueia.
- Se já existe execução em andamento, retorna **409** com qual etapa está rodando.
- Se as dependências não estão satisfeitas, retorna **422** com quais faltam.
- Se a etapa está `concluida` e a ação é `atualizar`, executa o incremental.
  Se a ação é `refazer`, exige `confirmar: true` no corpo.
- `POST .../cancelar` marca `cancelada` e sinaliza o subprocesso. A retomada garante que nada
  se perde — cancelar é seguro por construção.
- Erros retornam `{erro: {codigo, mensagem, detalhe}}`, com `codigo` estável e legível
  (`EXECUCAO_EM_ANDAMENTO`, `DEPENDENCIA_NAO_SATISFEITA`, `TETO_CUSTO_EXCEDIDO`,
  `PROVEDOR_INDISPONIVEL`).

## 4. Interface web — telas

### 4.1 Hub do run (a tela principal)

Modelo mental: **pipeline do GitLab**. `run → etapas → artefatos`.

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Run #42 · "Atualização agosto"      modo: assistido    custo: US$ 3,17     │
│ config v7 · criado 16/08 09:12 · aberto                    [abortar run]   │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│  ✓ 0a ──► ✓ 1 ──► ✓ 2 ──► ▶ 3 ──► ○ 4 ──► ○ 5 ──► ○ 6a ─► ○ 6b ─► ○ 6c ... │
│  catálogo termos coleta  classif.  corte  extração  pares  rerank  llm     │
│                                                                            │
├────────────────────────────────────────────────────────────────────────────┤
│ ▶ Etapa 3 — Classificar itens                        executando            │
│   ████████████████░░░░░░░░  18.402 / 31.887   ·  erros: 12  ·  US$ 1,84    │
│   iniciada há 14 min · previsão: ~11 min       [ver log]  [cancelar]       │
└────────────────────────────────────────────────────────────────────────────┘
```

Estados por nó, com cor e ícone distintos:

| Estado | Ícone | Significado |
|---|---|---|
| `nao_iniciada` | ○ | dependências podem ou não estar satisfeitas |
| `executando` | ▶ | com barra de progresso ao vivo |
| `aguardando_aprovacao` | ⏸ | gate pendente — destaque forte |
| `concluida` | ✓ | |
| `desatualizada` | ⚠ | fingerprint divergente de alguma dependência |
| `falhou` | ✗ | com mensagem |
| `cancelada` / `pulada` | ⊘ | |

Nó clicável → tela da etapa. Atualização por HTMX (`hx-get` a cada 2s no fragmento do grafo)
ou SSE quando há etapa executando.

### 4.2 Tela da etapa

- **Cabeçalho:** estado, ação usada, duração, custo, fingerprint.
- **Progresso:** barra + processados/total + estimativa de término.
- **Ações:** `Atualizar` (principal) · `Retomar` (se falhou) · `Refazer` (com confirmação e
  custo estimado) · `Cancelar` (se executando).
- **Parâmetros:** formulário gerado a partir do `Params` Pydantic. Mostra qual camada definiu
  cada valor (default / config / override).
- **Métricas:** o `metricas` JSON renderizado — o resumo que o usuário analisa antes do próximo play.
- **Erros:** tabela de `erro_item` com botão "reprocessar pendentes".
- **Log:** streaming SSE, filtro por nível, busca.
- **Artefatos:** amostra do resultado + link para a listagem completa.

### 4.3 Tela de gate

O gate é um objeto de primeira classe, não um "pausar":

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⏸ Aprovação necessária — Etapa 3: Classificar itens                  │
├──────────────────────────────────────────────────────────────────────┤
│ Escopo          31.887 textos únicos  (de 214.503 itens novos)       │
│ Chamadas        31.887                                                │
│ Modelo          inclusionai/ling-2.6-flash  (barato)                 │
│ Custo estimado  US$ 3,20        Teto do run: US$ 10,00               │
│ Duração         ~25 min                                               │
├──────────────────────────────────────────────────────────────────────┤
│ Amostra do que será processado:                          [ver todos] │
│  ...                                                                  │
├──────────────────────────────────────────────────────────────────────┤
│ [Aprovar e executar]  [Editar parâmetros]  [Pular etapa]  [Abortar]  │
└──────────────────────────────────────────────────────────────────────┘
```

Gate da **etapa 1** é especial e merece tela própria: lista de termos com os códigos que cada um
atende, botão **"testar"** por termo (consulta o PNCP e devolve a contagem antes de aceitar), e
ativar/desativar individual. É o gate de maior alavancagem do sistema.

Toda edição vira `run_etapa.params_override` — senão retomar depois desfaz a edição em silêncio.

### 4.4 Dashboard de custo

- Custo por run, por etapa, por capacidade, por modelo.
- Série temporal e acumulado no mês.
- Alerta visual ao aproximar do teto.
- Tabela de "mais caros": quais etapas e quais documentos consumiram mais.

Dado a restrição nº 1 do projeto, isto não é métrica de vaidade — é instrumento de controle.

### 4.5 Telas de configuração

- **Parâmetros** — formulário por etapa, gerado do Pydantic. Salvar cria `config_versao` nova.
- **Prompts** — editor com histórico, diff entre versões, ativar versão.
- **Provedores** — URL (inclusive a do túnel da GPU, que muda de tempos em tempos), modelo,
  batch, teto, botão testar. Health check inline.
- **Termos** — busca, ativar/desativar, testar contra o PNCP.

### 4.6 Telas de resultado

- **Exports** — lista com download, nº de linhas, run de origem.
- **Diff entre runs** — o que mudou: item novo, item sumiu, preço alterado.
- **Rastreio de item** — a consulta de auditoria renderizada: do export até a URL no PNCP,
  passando por classificação (com prompt e modelo), extração (com estratégia) e pareamento
  (com scores). É o teste de fogo do projeto inteiro.

## 5. Autenticação

Escopo mínimo deliberado: usuário único operando, alguns observadores.

- Token único em header (`X-API-Token`) para `/api/*`.
- Sessão por cookie para a web, com senha única em variável de ambiente.
- **Não construir** cadastro de usuários, papéis, SSO ou recuperação de senha.
- `aprovado_por` e `criado_por` guardam um nome livre, informado na sessão. Serve para
  auditoria, não para controle de acesso.

Se um dia houver necessidade real de múltiplos usuários com permissões, isso é uma decisão nova
— não antecipar.

## 6. Notificações

Etapa concluída, etapa falhou, gate aguardando, teto de custo atingido.

Canais: e-mail (SMTP) e/ou Telegram (bot). Configurável por evento.

Pipeline em background sem aviso vira pipeline esquecido — este item é o que fecha a lacuna
deixada por não ter mais o usuário olhando o terminal.
