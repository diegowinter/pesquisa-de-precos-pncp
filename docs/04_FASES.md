# 04 — Fases evolutivas

## Princípio de sequenciamento

Cada fase é **útil sozinha** e **deixa o sistema funcionando**. Nenhuma fase exige que a
seguinte exista para valer a pena. Isso permite parar, reordenar ou cortar escopo sem deixar o
projeto pela metade.

As fases 0 e 1 são **reversíveis e não quebram o fluxo diário do usuário**. O risco real começa
na fase 2.

```
F0 Fundação ──► F1 Núcleo ──► F2 Banco ──► F3 Execução ──► F4 API ──► F5 Web
                                              │                          │
                                              └──► F6 Config/Prompts ◄───┘
                                                        │
                                              F7 Provedores ──► F8 Etapa 5 dupla
                                                                      │
                                                                F9 Qualidade e operação
                                                                      │
                                    F10 Banco total ──┬──► F12 Deploy em servidor
                                                      │
                                    F11 Processamento externo

                                    F10 ──► F13 Consolidação web-only
```

As fases 0-9 assumem execução na máquina do usuário. As fases 10-12 mudam essa premissa: o
banco passa a ser o único meio de persistência (ADR-018), todo processamento pesado vira serviço
externo (ADR-019) e a pipeline roda em servidor. F10 e F11 são independentes entre si e podem
ser feitas em paralelo; F12 depende das duas.

A F13 recolhe o andaime: com o banco já sendo o default, ela remove o caminho CSV, a CLI e o
segundo processo, deixando a web como superfície única (ADR-020). Depende só da F10.

---

## Fase 0 — Fundação estrutural

**Objetivo:** mover o código para a estrutura alvo **sem mudar uma linha de lógica**.

### Entrega
1. Criar a árvore de [01_ARQUITETURA.md §7](01_ARQUITETURA.md#7-estrutura-de-pastas-alvo).
2. **`config/paths.py`** — centralizar todos os caminhos hoje literais dentro dos scripts.
   *Isto vem primeiro, antes de mover qualquer arquivo.* Sem isso, mover pastas vira caça a bug.
3. Mover `0a_*.py … 8_*.py` para `etapas/` com os nomes novos (`e0a_catalogo.py` etc.),
   mantendo o corpo intacto.
4. Mover `scripts/*.py` para `core/` e `providers/` conforme a natureza de cada um.
5. `legado/` sai do repositório (vira tag git). `ferramentas/` permanece.
6. `pyproject.toml` com o pacote instalável (`pip install -e .`), ruff + pytest configurados.

### Não fazer nesta fase
Refatorar lógica, mudar assinaturas, tocar em CSV, introduzir banco.

### Critério de aceite
O usuário roda o ciclo `--atualizar` completo pelos comandos novos e obtém **exatamente** os
mesmos arquivos que obtinha antes. Diferença byte a byte nos CSVs de saída.

### Risco
Baixo. Reversível com `git revert`.

---

## Fase 1 — Núcleo executável

**Objetivo:** extrair a função `executar()` de cada etapa e criar o registry. O CLI vira casca.

### Entrega
1. `etapas/base.py` — `ContextoExecucao`, `ResultadoEtapa`, `Estimativa` (ver [03_ETAPAS.md](03_ETAPAS.md)).
2. Para cada etapa: `Params` (Pydantic), `executar(params, ctx)`, `estimar(params, ctx)`.
   O `main()` do script vira um wrapper de ~10 linhas.
3. `etapas/registry.py` com a tabela de metadados e dependências.
4. `cli/` com Typer — flags **geradas a partir dos `Params`**, não escritas à mão.
5. `ContextoExecucao` de console: `progresso()` alimenta o `rich.Progress` existente,
   `log()` imprime, `erro_item()` grava em `data/erros/` como hoje.

### Critério de aceite
- `python -m pesquisa_precos.cli etapa 3 --concurrency 8` funciona igual ao script antigo.
- `python -m pesquisa_precos.cli estimar 3` responde quantos textos e quanto custaria — **sem
  gastar nada**.
- `python -m pesquisa_precos.cli grafo` desenha a ordem a partir do registry.

### Por que antes do banco
Porque é o que permite o mesmo código rodar via CLI e via API depois. Fazer isso **depois** do
banco significaria mexer em cada etapa duas vezes.

### Risco
Médio-baixo. Ainda escrevendo CSV; o usuário continua rodando normalmente.

---

## Fase 2 — Persistência em PostgreSQL

**Objetivo:** o banco vira a fonte de verdade. É a fase de maior risco do projeto.

### Entrega
1. `db/` com SQLAlchemy 2.x + Alembic. DDL de [02_SCHEMA.md](02_SCHEMA.md) na migration inicial.
2. Repositórios por agregado (`db/repos/documento.py`, `item.py`, `par.py`…).
   **Nenhuma etapa escreve SQL solto.**
3. `migracao/` — scripts one-shot CSV → Postgres, idempotentes e resumíveis.
   Ver [05_MIGRACAO.md](05_MIGRACAO.md).
4. Cada etapa passa a ler/escrever pelos repositórios.
5. Política de retenção implementada (`documento_pagina` é o gigante: 2,6 GB).

### Ordem sugerida dentro da fase
Migrar por agregado, validando cada um antes do próximo:
`catalogo` → `termo` → `documento`+`item` → `classificacao` → `enriquecido` → `par` → `grupo`.

### Critério de aceite
- Contagem de linhas no banco bate com os CSVs (números em [02_SCHEMA.md §1](02_SCHEMA.md#1-dimensionamento-medido-no-acervo-atual-2026-08-16)).
- Etapas 7 e 8 rodam **direto do banco** e produzem um XLSX idêntico ao último export oficial.
  Esse é o teste de regressão real: mesma entrada, mesma saída.
- A consulta de auditoria de [02_SCHEMA.md §12](02_SCHEMA.md#12-consulta-de-auditoria-teste-de-fogo-do-schema) roda e faz sentido.

### Risco
**Alto.** Mitigações obrigatórias:
- CSVs originais preservados intactos durante toda a fase (não apagar nada);
- migração idempotente — rodar duas vezes não duplica;
- `pg_dump` antes de cada lote grande.

---

## Fase 3 — Execução observável

**Objetivo:** runs, etapas, progresso, retomada, custo e log. O terminal deixa de ser necessário.

### Entrega
1. Tabelas `run`, `run_etapa`, `run_log`, `erro_item`, `llm_chamada`, `execucao_lock`.
2. `runner/` — cria o subprocesso, adquire o lock (linha + `pg_advisory_lock`), injeta o
   `ContextoExecucao` de banco, escreve heartbeat, trata cancelamento e morte de processo.
3. **Lease com expiração**: `heartbeat_em + timeout` devolve à fila o que ficou preso.
4. Contabilidade de custo: todo adapter de provedor grava em `llm_chamada`.
5. **Teto de custo por run** — `ctx.gastar()` levanta `TetoDeCustoExcedido` e pausa a etapa.
6. Cálculo e gravação do `fingerprint`; detecção de etapa `desatualizada`.
7. Log estruturado (JSON com `run_id`/`etapa` em toda linha) gravado em `run_log`.

### Critério de aceite
- Matar o processo no meio de uma etapa e retomar não perde nem duplica trabalho.
- `SELECT * FROM run_etapa` mostra progresso ao vivo enquanto a etapa roda.
- Custo acumulado por etapa e por run é consultável.
- Um teto baixo de propósito interrompe a etapa de forma limpa.

### Risco
Médio. A parte delicada é a transacionalidade (resultado + estado no mesmo commit).

---

## Fase 4 — API

**Objetivo:** comandar e observar por HTTP.

### Entrega
1. FastAPI, rotas `/api/*` (ver [06_API_E_WEB.md](06_API_E_WEB.md)).
2. `services/` entre as rotas e os repositórios — **web e API consomem os mesmos serviços**.
3. Leitura: runs, etapas, progresso, log (SSE), custo, erros, resultado, auditoria.
4. Comando: criar run, executar etapa, cancelar, aprovar gate, editar override.
5. `GET /api/health` e `GET /api/providers/status`.
6. Autenticação simples (token único ou basic auth). Não construir gestão de usuários.

### Critério de aceite
Ciclo completo por `curl`: criar run → executar 0a → ver progresso → aprovar gate da 1 →
executar 2 → cancelar no meio → retomar.

### Risco
Baixo.

---

## Fase 5 — Interface web

**Objetivo:** o "hub" de execução estilo GitLab Pipelines.

### Entrega
1. Jinja2 + HTMX + Alpine.js (CDN inlined, sem bundler).
2. **Tela de run** — grafo das etapas com estado por nó
   (`✓ concluída`, `⚠ desatualizada`, `⏸ aguardando`, `▶ executando`, `✗ falhou`).
3. **Tela de etapa** — progresso ao vivo, log, métricas, erros, ações
   (atualizar / retomar / refazer / cancelar).
4. **Tela de gate** — preview, estimativa de custo, edição, aprovar/pular/abortar.
5. **Dashboard de custo** — por run, por etapa, acumulado no mês.
6. Download dos exports.

### Critério de aceite
O usuário conduz um ciclo `--atualizar` inteiro **sem abrir o terminal**.

### Risco
Baixo. Falha aqui é cosmética, não corrompe dados.

---

## Fase 6 — Configuração e prompts no banco

**Objetivo:** parametrizar sem deploy, com histórico.

### Entrega
1. `config_versao` / `config_valor` — imutáveis; editar cria versão nova. Run aponta para uma.
2. Resolução em camadas (default do schema ← config ← override do run), gravada em
   `run_etapa.params_efetivos`.
3. `prompt` / `prompt_versao` — prompts migrados de `core/prompts.py` para o banco,
   com versão ativa e histórico. `llm_chamada.prompt_versao_id` passa a ser preenchido.
4. Formulários gerados a partir dos `Params` Pydantic — validação idêntica em CLI, API e web.
5. Tela de diff entre versões de config e de prompt.

### Critério de aceite
- Mudar `rejeitor_threshold` pela interface, rodar em modo `amostra`, comparar o resultado,
  e conseguir dizer exatamente qual versão de config produziu cada run.
- Nenhum valor gravado pela interface passa pela validação do Pydantic sem ser checado.

### Risco
Médio. A armadilha é config virar bagunça — mitigada pela imutabilidade e pela validação.

---

## Fase 7 — Provedores plugáveis

**Objetivo:** trocar GPU caseira ↔ serviço pago por capacidade, sem tocar em código de etapa.

### Entrega
1. `providers/` com quatro `Protocol`: `chat`, `embed`, `rerank`, `ocr`.
2. Adapters: `gpu_caseira`, `lm_studio`, `openrouter`, `openai_compat`, `ocr_local`.
   Cada um declara `batch_size`, `rpm_limite`, custo por Mtok, e faz seu retry/backoff.
3. Tabelas `provedor`, `capacidade_provedor`, `provedor_status`.
4. **Chave de cache de embedding passa a incluir `provedor + modelo + dimensao`** — migration
   obrigatória, senão espaços vetoriais se misturam em silêncio.
5. **Fallback proibido em `embed`** (falha e para a etapa). Permitido em `chat`/`rerank`/`ocr`.
6. Health check por provedor, executado antes de qualquer play.
7. Tetos de custo por capacidade.

### Critério de aceite
- Trocar o provedor de `chat` pela interface e rodar a etapa 3 em modo `amostra` sem mudar código.
- Derrubar a GPU de propósito: `embed` falha com mensagem clara e **não** cai para outro provedor.
- Health check detecta o túnel caído antes do play, não 40 minutos depois.

### Risco
Médio. O ponto crítico é o cache de embeddings — errar ali gera bug silencioso e caro.

---

## Fase 8 — Etapa 5 com duas estratégias e roteamento

**Objetivo:** implementar o desenho de extração e capturar os 38% de economia medidos.

> Depende da Fase 3 (custo medido) e da Fase 7 (provedores). Pode ser antecipada se o retorno
> financeiro pesar mais que a ordem — mas sem medição de custo é impossível validar o ganho.

### Entrega
1. Reordenar o fluxo: **etapa 2 não baixa mais PDF**; download vai para a etapa 5, depois do corte.
2. Estratégia `janela` portada com a validação atual **integralmente preservada**
   (confirmação por quantidade, banda de sanidade, `doc_status`).
3. Estratégia `completa` com os quatro requisitos herdados
   ([03_ETAPAS.md §5.2](03_ETAPAS.md#52-estratégia-completa)), incluindo chunking com overlap
   para os 5,6% de documentos acima de 40k tokens.
4. Roteamento `auto` com a fórmula parametrizada em `config_valor`.
5. **Descarte do PDF** após extração; `url_pncp` + `hash_arquivo` + `n_paginas` preservados.
6. `documento_extracao` com custo por documento e por estratégia.
7. Ação "reprocessar este documento com outra estratégia" na interface.
8. Estratégia `visao` como rota de exceção (`doc_status` suspeito/ilegível + muitos itens).

### Critério de aceite
- Amostra de ~500 documentos processada pelas duas estratégias: comparar taxa de confirmação,
  divergência de preço e custo real. A `completa` **não pode** ter taxa de confirmação pior.
- Economia real medida vs. os 38% previstos (o desvio é informação, não falha).
- Nenhum PDF permanece em disco após a etapa concluir.

### Risco
**Alto para a qualidade dos dados.** Mitigações: rodar as duas em paralelo sobre uma amostra
antes de trocar o padrão; manter `documento_pagina` para permitir reprocesso sem rebaixar.

---

## Fase 9 — Qualidade e operação

**Objetivo:** o que substitui "o usuário olhando o terminal" e sustenta o dia a dia.

### Entrega
1. **Conjunto de regressão** — ~200 itens com rótulo conhecido, extraídos da tabela `rotulo`.
   Roda contra qualquer mudança de modelo, threshold ou prompt e reporta precisão/recall.
   Sem isso, trocar de provedor é no escuro.
2. **Diff entre runs** — "o que mudou do export de ontem para o de hoje": preço caiu, item novo,
   item sumiu. Generalização do `--novos` atual. Provavelmente a feature de maior valor para o
   usuário final, não só para a operação.
3. **Notificações** — etapa concluída / falhou / gate esperando, via e-mail ou Telegram.
   Pipeline em background sem aviso vira pipeline esquecido.
4. **Backup** — `pg_dump` diário + verificação de restauração.
5. **Testes onde pagam** — não buscar cobertura. Focar em: parsers da API do PNCP (muda sem
   aviso), extração de item de texto, e lógica de agrupamento/menor preço. São os pontos onde
   bug silencioso vira preço errado no export.
6. **Recalibração de thresholds** pela interface, usando `rotulo`.

### Critério de aceite
- Suite de regressão roda em < 5 min e reprova uma degradação introduzida de propósito.
- Diff entre dois runs é consultável na interface.
- Uma falha de etapa gera notificação em menos de 1 min.

### Risco
Baixo. Tudo aditivo.

---

---

## Fase 10 — Banco como fonte da verdade nas etapas 0a–6c

**Objetivo:** eliminar o CSV como meio de persistência das etapas. É o pré-requisito de
qualquer execução em servidor (ADR-018).

> As etapas 7 e 8 já fizeram esse caminho na Fase 2 e servem de referência viva. O pacote
> `migracao/` (17 passos CSV → Postgres) documenta o mapeamento de cada CSV para cada tabela —
> é a especificação que já existe, escrita e validada.

**Padrão:** `--fonte banco|csv` como nas etapas 7/8, com **`banco` como default** (a web só
chama por ele). O `csv` permanece como escape hatch para rodar fora do servidor, e como
rollback se uma etapa der problema no meio de uma execução real.

> **Revisto na Fase 13 (ADR-020):** o escape hatch cumpriu o papel e SAIU. O rollback deixou de
> ser uma flag e passou a ser o repositório congelado `../pipeline-csv-congelado/`.

### Entrega

**Bloco A — schema (o único desenho novo)**
1. `catalogo_raw` — CATMAT/CATSER completos (ADR-017). Hoje só existe em parquet.
2. `pdm_permitido` — allow-list saindo de `core/catalogo/local.py` (ADR-017).
3. `catalogo_item` passa a ser **derivado** de `catalogo_raw ∩ pdm_permitido` por SQL.
4. `export.arquivo` (caminho) → `export.conteudo` (`bytea`) (ADR-018 §2).
5. **Escritor de banco comum** (`db/repos/escrita.py`) — o equivalente SQL do
   `core.io_seguro.EscritorSeguro`: inserção em lote com `ON CONFLICT`, contadores e commit
   por lote. Feito **antes** do Bloco B, para não ser reinventado dez vezes.

**Bloco B — etapas 0a e 1** (volume pequeno; fixa o padrão)
6. 0a grava `catalogo_raw`, aplica `pdm_permitido`, deriva `catalogo_item`; o delta vira
   comparação com `catalogo_snapshot`.
7. 1 grava `termo` + `termo_codigo` (tabelas já existentes). Checkpoint = `SELECT` dos códigos
   já processados.

**Bloco C — etapas 2, 3 e 4** (o grosso do volume)
8. 2 grava `documento`, `item` e `documento_termo` — este último elimina a gambiarra do
   `2_conceitos_extra.csv`. Watermark sai de CSV e vira `coleta_watermark`.
9. 3 grava `texto_classificacao` (o cache caro, por `texto_hash`) e `item_categoria`.
   **Ganho estrutural:** o dedup de ~5x deixa de ser intra-execução e vira permanente.
10. 4 vira `UPDATE item SET sobrevivente = true WHERE ...`. Não há tabela de sobreviventes:
    "sobrevivente" é atributo, não conjunto. Some um CSV de 182 MB.

**Bloco D — etapas 5 e 6**
11. 5 grava `documento_pagina`, `item_enriquecido`, `documento_extracao`; checkpoint =
    `documento.estado`.
12. 6a/6b/6c gravam na tabela `par` única (ADR-013) e em `rotulo`. `embedding_cache` substitui
    o parquet — que é justamente o arquivo que obrigaria um volume persistente.
13. 8 gera o XLSX em `BytesIO` e grava em `export.conteudo`; a web serve os bytes.

### Critério de aceite
- Uma execução completa 0a → 8 com `--fonte banco` **não cria nenhum arquivo** em `data/`.
- Export produzido pelos dois caminhos (`csv` e `banco`) é idêntico célula a célula, salvo a
  divergência já conhecida e documentada da coluna `Unidade`.
- Matar o processo no meio de cada etapa e retomar não reprocessa o que já foi concluído nem
  duplica linha — o checkpoint por consulta é o que está sendo testado aqui.
- `pytest` e `ruff check pesquisa_precos` limpos.

### Risco
**Alto.** É a fase que mexe em todas as etapas do pipeline de uma vez. Mitigações: `--fonte
csv` preservado o tempo todo (até a Fase 13); ordem do grafo (A→B→C→D), nunca fora dela — fazer a 3 antes da 2
exigiria um adaptador temporário lendo CSV para alimentar tabela; `pg_dump` antes de cada
bloco, como na Fase 2.

---

## Fase 11 — Externalização total do processamento

**Objetivo:** o container fica com orquestração e estado; todo processamento pesado vira serviço
externo (ADR-019). É o que permite a máquina do servidor ser pequena.

> Pré-requisito do Bloco D da Fase 10 no que toca a etapa 5 — mas independente dos blocos
> A-C, e pode ser feita em paralelo.

### Entrega
1. **Capacidade `pdf`** — `ProvedorPdf` em `providers/protocolos.py`, ao lado de `ProvedorOcr`;
   entrada no enum `capacidade` e em `providers/resolver.py`.
2. **o serviço `pdf`** (hoje `servicos/pdf.py`, em `../pncp-servicos-locais`) no padrão do `ocr`: baixa o PDF, extrai texto nativo por
   página, detecta escaneada (limiar de 100 chars), rasteriza a 200 DPI e chama o OCR
   internamente. Devolve `{paginas: [{pagina, texto, densidade, escaneada}], n_paginas, hash}`.
3. **Capacidade `pareamento`** — recebe catálogo + itens, calcula BM25 e cosseno, aplica o
   corte top-K + piso **em streaming** e devolve só os sobreviventes.
4. Registrar as capacidades novas nas etapas 5 e 6a em `etapas/registry.py`, para que o health
   check pré-play do executor as cubra.
5. **Remover do container:** `pymupdf`, `rank-bm25`, `sentence-transformers`, `numpy`,
   `pandas`. O que restar de uso local vai para um extra opcional do `pyproject.toml`
   (`[project.optional-dependencies] localmente`), para o desenvolvimento local continuar
   funcionando.

### Critério de aceite
- A etapa 5 processa uma amostra de documentos com o `pymupdf` **desinstalado** do ambiente.
- A 6a produz os mesmos pares sobreviventes que o caminho local, sobre a mesma entrada.
- Serviço externo derrubado de propósito reprova a etapa no health check, **antes** de começar.

### Risco
Médio. A qualidade da extração e do pareamento não muda — é o mesmo código, do outro lado de
um HTTP. O risco real é operacional: dependência de disponibilidade e de endereço estável.

---

## Fase 12 — Deploy em servidor (Railway)

**Objetivo:** a pipeline inteira rodando em nuvem, operada pela web, sem a máquina do usuário.

> Depende das Fases 10 e 11. Tentar antes exigiria volume persistente e uma imagem com torch —
> exatamente o que as duas fases anteriores eliminam.

### Entrega
1. **Dockerfile explícito** (não Nixpacks, que erra com `uv` + `pyproject`) instalando só as
   dependências de orquestração.
2. `alembic upgrade head` como release command — **schema sim, dados não**: a execução em
   nuvem começa do zero, sem migrar o acervo local.
3. Bind em `$PORT` (hoje `__main__.py` fixa `WEB_PORT=8001`).
4. Normalização do `DATABASE_URL`: o provedor entrega `postgresql://`, o código exige
   `postgresql+psycopg://`.
5. **Segurança não-opcional em produção:** `WEB_SENHA` obrigatório (vazio desliga o login,
   ver `web/auth.py`) e `SECRET_KEY` próprio para o `SessionMiddleware`.
6. Segredos como variáveis de ambiente: `OPENAI_API_KEY`, `RESEND_API_KEY` e as URLs das
   capacidades externas.
7. Retomada após restart: `recuperar_travados()` devolve à fila o `run_etapa` cuja lease
   expirou, e o checkpoint por consulta (Fase 10) garante que ele retome de onde parou.

### Critério de aceite
- Redeploy no meio de uma etapa em execução: ao voltar, a etapa retoma sem reprocessar o
  concluído e sem duplicar linha.
- Nenhum arquivo escrito no container durante uma execução completa.
- Login exigido; nenhuma rota de comando acessível sem sessão.

### Risco
Médio. Reversível pelo snapshot congelado (`../pipeline-csv-congelado/`) — até a Fase 13 a
reversão era o fluxo local por `--fonte csv`, que não existe mais.

---

## Fase 13 — Consolidação web-only

**Objetivo:** uma superfície de operação só. Um processo, uma porta, um meio de persistência.
O sistema inteiro é operado pelo navegador — inclusive rodando na máquina do usuário.

> Depende da Fase 10 (que fez `banco` virar o default nas 12 etapas). É a fase que RECOLHE o
> andaime das anteriores: os dois caminhos existiam para migrar com segurança, não para durar.
> Ver ADR-020 para o raciocínio completo.

### Entrega
1. **Um processo, uma porta.** Os routers de `api/routers/` são montados na app da web sob
   `/api`; `api/app.py`, `api/__main__.py` e `web/__main__.py` saem. Entrada única:
   `python -m pesquisa_precos`.
2. **A CLI sai** — `cli/`, `rodar.py`, `limpar.py`, `rodar_etapa3.ps1` e o `main()` de cada
   etapa. `typer` sai das dependências. `runner/processo.py` vira o único ponto de entrada de
   execução, subido como subprocesso pela web (ADR-002).
3. **`--fonte csv` sai das 12 etapas**, junto com os params que só existiam para ele
   (`--refiltar` na 6a, `--entrada-legado` na 3). `VERSAO_CODIGO` vai a 2.0.0 em todas.
4. **Some o que só servia ao CSV:** `core/io_seguro.py`, o carregador de parquet de
   `core/catalogo/local.py`, `texto_catalogo`/`descricao_itens` de `core/textos.py`,
   `registry.caminho_erros` e o `ContextoConsole` (que vira `ContextoNulo`, para `estimar()`).
5. **`config/paths.py` fica**, reduzido ao que `migracao/` lê — é o mapa do acervo ainda não
   migrado. O teste estrutural inverte: de "todo caminho da etapa cai dentro de `data/`" para
   "**nenhuma** etapa expõe caminho".
6. **Telas para o que só a CLI tinha:** `/provedores` (era `cli providers saude`) e, na tela
   da etapa, o que fica desatualizado ao refazer (era `cli grafo`). O download de export passa
   a servir `export.conteudo`, a última rota que dependia do disco.

### Pré-requisito operacional
**Migrar o acervo antes de usar em produção.** O banco tem o schema mas quase nenhum dado de
domínio; os 1,6 milhão de itens vivem só nos CSVs. `migracao/` continua intacto justamente
para isso. Remover o código do CSV não impede a migração (o importador tem leitores próprios),
mas depois dela não sobra mais nada para comparar os dois caminhos.

### Critério de aceite
- `python -m pesquisa_precos` sobe **um** processo; `localhost:8001` opera um ciclo 0a → 8
  inteiro sem tocar em terminal.
- Uma execução completa não cria nenhum arquivo em `data/`.
- `grep -rn "typer" pesquisa_precos/` não devolve nada, e nenhum módulo de `etapas/`,
  `runner/`, `web/` ou `api/` tem `argparse`.
  (Os três `main()` de `argparse` em `core/coleta/` FICAM: são sondas manuais da API do
  PNCP — `--termo`, `--tipo`, um número de controle — read-only, sem relação com run,
  banco ou custo. São ferramenta de diagnóstico, não superfície de operação.)
- `pytest` e `ruff check pesquisa_precos` limpos.

### Risco
Médio-baixo em código (é remoção, e o que fica já era o default desde a Fase 10), **alto em
operação**: sem banco de pé não há pipeline, e não sobra caminho degradado. O rollback é o
repositório congelado, não uma flag.

## Fase 14 — Configuração plugável: tudo pela tela, nada no `.env`

> **Depende da Fase 7** (que criou `provedor`/`capacidade_provedor` e a resolução por
> capacidade) e da **Fase 6** (`config_versao` versionada). É a fase que faz as duas
> **chegarem ao operador**: hoje as tabelas existem, a resolução existe, e nenhuma rota escreve
> nelas — a configuração real da aplicação segue num `.env` editado à mão.

**Princípio:** o que é *configuração de operação* mora no banco e se edita pela tela; o `.env`
guarda só o *bootstrap* — o que a aplicação precisa saber antes de conseguir ler o banco.

| Sai do `.env` (vira tela + banco) | Fica no `.env` |
|---|---|
| `OPENAI_*`, `LOCAL_*` (modelo, base_url, chave) | `DATABASE_URL` |
| `GPU_*`, `PDF_*`, `PAREAMENTO_*` | `APP_SECRET_KEY` (chave-mestra da cifra) |
| `EMBEDDER_MODEL`, `RERANKER_MODEL` | |
| `REJEITOR_THRESHOLD`, `RERANK_T_*`, `MIN_ITENS`, `TOP_N` | |
| `CUSTO_USD_CHAMADA_*` | |

**Blocos**

1. **Cifra de segredo** ([ADR-022](07_DECISOES.md#adr-022)). Dep nova `cryptography`; migração
   Alembic troca `provedor.api_key_ref` por `api_key_cifrada`/`api_key_last4`/`api_key_key_id`;
   helper `db/segredo.py` (cifrar/decifrar/rotacionar). A app recusa subir sem `APP_SECRET_KEY`.
2. **CRUD de provedores na tela.** `/provedores` deixa de ser só leitura: cadastrar/editar
   provedor (nome, capacidades, `base_url`, modelo, `batch_size`, `rpm_limite`, custo/Mtok,
   chave write-only) e apontar `capacidade → provedor`. Botão "testar agora" reusando
   `providers/saude.py`. Passa por `services/`, nunca por `db/repos` direto.
3. **Thresholds viram `Params`.** ✅ `rerank_t_aceita`/`rerank_t_rejeita` (6b) e
   `min_itens`/`top_n` (7 — o fallback para `ctx.config` saiu). Ganham formulário de graça
   (`schema_parametros`) e entram em `config_versao`. `VERSAO_CODIGO` de ambas foi para
   `2.1.0`: o valor efetivo mudou de origem, e o fingerprint precisa enxergar isso.
   `rejeitor_threshold` **não migrou porque já estava morto** — a 6a usa o `Param` `piso`
   desde a Fase 11 e nada lia a variável; ela só saiu de `carregar_config()`.
4. **Seed + remoção do fallback.** Migração semeia `provedor`/`capacidade_provedor` e uma
   `config_versao` inicial a partir do `.env` de hoje (uma vez, sem recadastro manual); depois
   `_resolver_via_env` sai de `providers/resolver.py` e `carregar_config()` encolhe.
   `tests/test_estrutura.py` ganha a guarda: nenhum módulo de `etapas/` lê threshold de
   `ctx.config`.

**Critério de aceite**

Com o `.env` reduzido a `DATABASE_URL` + `APP_SECRET_KEY`, o operador troca o modelo do LLM, a
URL do túnel da GPU e um threshold **inteiramente pela tela**, roda uma etapa, e o `run` aponta
para a `config_versao` que registra exatamente esses valores. Nenhuma chave de API aparece em
resposta de API, em HTML ou em `pg_dump` legível.

**Risco**

Médio. O bloco 4 é a virada dura (sem provedor cadastrado, nada roda) e vem **por último**, com
os blocos 1-3 já validados. Reversível: o `downgrade` da migração restaura `api_key_ref` e o
`.env` do operador continua no disco durante a transição.

## Fora de escopo (todas as fases)

Registrado para que ninguém "melhore" o projeto nessa direção:

autenticação complexa / SSO · multi-tenant · Kubernetes · fila de mensagens ·
internacionalização · aplicativo móvel · front-end SPA separado ·
paralelismo entre runs · auto-avanço de etapas.

**Revisto nas fases 10-12** (o que estava nesta lista e deixou de estar):
- *Docker* deixa de ser "obrigatório" proibido e passa a ser o meio de deploy da F12. O que
  continua fora é **Kubernetes** e qualquer orquestração de containers.
- *Microserviços* — os serviços da F11 (`pdf`, `pareamento`, e os já existentes de LLM/embed/
  rerank/OCR) **não** são microserviços no sentido proibido: não têm estado, não têm banco
  próprio, não se chamam entre si e não participam do domínio. São executores de processamento
  stateless atrás de HTTP. O estado e a orquestração continuam num processo só (ADR-001).

## Resumo de esforço relativo

| Fase | Peso | Risco | Reversível |
|---|---|---|---|
| F0 Fundação | ▪ | baixo | sim |
| F1 Núcleo | ▪▪ | médio-baixo | sim |
| F2 Banco | ▪▪▪▪ | **alto** | com backup |
| F3 Execução | ▪▪▪ | médio | sim |
| F4 API | ▪▪ | baixo | sim |
| F5 Web | ▪▪▪ | baixo | sim |
| F6 Config/Prompts | ▪▪ | médio | sim |
| F7 Provedores | ▪▪ | médio | sim |
| F8 Etapa 5 dupla | ▪▪▪▪ | **alto (dados)** | com amostra |
| F9 Qualidade | ▪▪ | baixo | sim |
| F10 Banco total | ▪▪▪▪▪ | **alto** | via `--fonte csv` |
| F11 Processamento externo | ▪▪▪ | médio | sim |
| F12 Deploy | ▪▪ | médio | sim |
| F13 Web-only | ▪▪▪ | médio-baixo | via repo congelado |
| F14 Config plugável | ▪▪▪ | médio | via `downgrade` da migração |
