# CLAUDE.md — guia de contexto para este repositório

Este é o repositório **oficial** da pesquisa de preços PLASEG via PNCP. Antes dele, o projeto
viveu como `itens-contratos-atas` (v1) e `itens-contratos-atas-v2` dentro do repo
`itens-via-script`; em 2026-08-16 a pasta `itens-contratos-atas-v3` foi **movida** (não copiada)
para cá e virou a raiz deste projeto. O desenho técnico completo do pipeline está em
[README.md](README.md) e [GUIA_IMPLEMENTACAO_PIPELINE.md](GUIA_IMPLEMENTACAO_PIPELINE.md) — leia
os dois antes de mexer em qualquer etapa. Este arquivo cobre o que eles não cobrem: como
trabalhar com o usuário, decisões já tomadas e o estado da validação.

Desde 2026-08-16 existe também **[docs/](docs/)** — o projeto de engenharia da transformação
desta pipeline em aplicação (banco, API, web). Se você vai implementar uma fase, comece por
[docs/README.md](docs/README.md) e [docs/08_CONVENCOES.md](docs/08_CONVENCOES.md).

## Estrutura: um pacote, uma superfície

```
pesquisa_precos/
  __main__.py  ← `python -m pesquisa_precos` sobe TUDO (HTML + /api) numa porta só
  config/    settings.py (só bootstrap, ver ADR-022) · paths.py (só do importador)
  steps/     e0a_catalogo · e1_termos · e2_collect · e3_classify · e4_cut
             e5_extract · e6a_pairs · e6b_rerank · e6c_validate · e7_group · e8_export
  core/      regras, parallel, prompts, collection (PNCP), catálogo, classification
  providers/ chat · embed · rerank · pdf · matching (resolver + adapters — TODOS clientes
             de serviço; nada roda em processo desde a ADR-021)
  strategies/ window · full · vision (implementações plugáveis da etapa 5)
  db/        models, repos, session (SQLAlchemy 2.x; migrations em alembic/)
  runner/    launcher, lock, fingerprint, worker, DbContext, NullContext
  services/  a camada que web e api compartilham — nenhuma rota fala com o banco direto
  api/       routers JSON, montados sob /api na app da web
  web/       app FastAPI + templates Jinja2 + static (HTMX/Alpine, sem bundler)
```

Cada etapa expõe `Params` (Pydantic) + `run(params, ctx)` + `estimate(params, ctx)`. A ordem e
as dependências vêm de `pesquisa_precos/steps/registry.py`; os `Params` geram o formulário de
configuração da web. Ao mudar a lógica de uma etapa, **bumpe o `CODE_VERSION` do módulo** — é o
que alimenta o fingerprint que marca as dependentes como desatualizadas.

### Idioma: identificador em inglês, texto em português

Desde 2026-08-22 os identificadores do código estão em inglês — pastas, módulos, funções,
classes, rotas HTTP e nomes de tabela e coluna. Ficam em português o vocabulário de nascença do
PNCP e da licitação (`item`, `termo`, `documento`, `catalogo`, `par`, `grupo`, `faixa_preco`) e
**todo texto que o operador lê**: labels do formulário, `description` dos `Field`, mensagens de
`ctx.log`, templates, comentários e docstrings.

A migration `0011` fez o rename no banco. Ela é reversível (`alembic downgrade`), e o mapa
completo do que mudou está no corpo dela.

### O pesado não roda aqui (ADR-021)

Desde 2026-08-22 existe `../pncp-servicos-locais/` — quatro serviços HTTP (`gpu`, `ocr`,
`pdf`, `pareamento`) com tudo que precisa de GPU ou de CPU intensiva: PyMuPDF, rasterização a
200 DPI, OCR, embedder, reranker, BM25 e o corte do produto catálogo × itens. Do lado de cá, a
capacidade que fala com o serviço de pareamento chama-se `matching`.

Aqui só ficaram **clientes**. Não existe mais `…EmProcessoAdapter`: `base_url` vazio é erro de
configuração, não "roda na própria máquina". A razão é a mesma da ADR-020 — dois caminhos para
o mesmo resultado divergem em silêncio — e o destino é um servidor econômico, que faz scraping
e escreve no banco e nada mais.

A linha do corte é **"precisa de GPU ou é CPU intensiva"**, não "toca em bytes": baixar o PDF
continua sendo daqui (é I/O, e o cliente do PNCP já existe para a etapa 2); o processo baixa e
manda os bytes por upload, o serviço devolve texto.

O companion **não importa `pesquisa_precos`** — é independente e tem `pyproject`, testes e
`.env` próprios. Consequências práticas: `OCR_*` saiu do `.env` daqui, a etapa 5 declara
`("pdf", "chat")` e a 6a declara `("matching",)`. Rodar o pipeline localmente é subir os
serviços também.

### Configuração não mora mais no `.env` (Fase 14, ADR-022)

Desde 2026-08-22 o `.env` tem **4 linhas**: `DATABASE_URL`, `APP_SECRET_KEY` e as duas
credenciais do Resend. Tudo que é configuração de operação foi para o banco e se edita pela
tela:

| O quê | Onde se configura |
|---|---|
| modelo, base_url, chave de API, batch, custo | `/providers` (tabela `provider`) |
| quem atende `chat`/`embed`/`rerank`/`pdf`/`matching` | `/providers` (`provider_capability`) |
| thresholds da 6b, `min_itens`/`top_n` da 7, todo `Params` | formulário da etapa (`config_versao`) |

Consequências para quem for mexer:

- **`carregar_config()` e `ctx.config` não existem mais.** Foram removidos em 2026-08-22: só
  transportavam um dict vazio. Configuração vem do banco, pelo `Params` da etapa ou pelo
  resolver de provedores.
- **Não existe fallback para o `.env`.** Capacidade sem provedor apontado levanta
  `CapabilityNotConfigured` e a etapa para antes de começar. Se uma etapa reprovar por isso, o
  conserto é cadastrar o provedor em `/providers`, não recriar a variável.
- **`APP_SECRET_KEY` é a chave-mestra** que cifra as chaves de API em
  `provider.api_key_encrypted` (AES-GCM, `db/secret.py`). Sem ela, nenhuma etapa que use LLM/GPU roda. O único ponto do
  código que decifra é `providers/resolver.py` — um teste estrutural guarda isso.
- **`tools/seed_providers.py`** foi a ponte de mão única do `.env` para o banco. Já
  rodou; não precisa rodar de novo.
- **Estado hoje:** `chat → openrouter`, `embed`/`rerank → gpu_caseira`, `lm_studio` cadastrado
  mas não apontado. **`pdf` e `matching` estão SEM provedor** — as etapas 5 e 6a não rodam
  até serem cadastradas.

### Não existe CLI (Fase 13, ADR-020)

Desde 2026-08-22 a web é a **única** superfície. Saíram: `pesquisa_precos/cli/`, `rodar.py`,
`limpar.py`, o `main()` de cada etapa, e o segundo processo da API. Saiu também o
`--fonte banco|csv`: **nenhuma etapa lê ou escreve arquivo**, o banco é o único meio de
persistência. `runner/processo.py` é o único ponto de entrada de execução, e quem o sobe é a
web, como subprocesso (ADR-002).

Consequência prática: **sem `DATABASE_URL` de pé não há pipeline.** Não sobrou caminho
degradado. O rollback é o repositório congelado (ver abaixo), não uma flag.

**`config/paths.py` não é mais "todos os caminhos do projeto"** — é o mapa dos CSVs que
`migracao/` ainda lê. Nenhum módulo de `etapas/` pode importá-lo, e `tests/test_estrutura.py`
guarda essa regra (ela era o inverso até a Fase 13).

## Fase 2 (banco) — implementada, migração ainda NÃO rodada

Desde 2026-08-16 existe o pacote `pesquisa_precos/db/` (SQLAlchemy 2.x + Alembic), o pacote
`migracao/` (17 passos CSV → PostgreSQL) e as etapas 7 e 8 com `--fonte banco`. O schema é o
de [docs/02_SCHEMA.md](docs/02_SCHEMA.md), criado por `alembic upgrade head` com DDL literal.

```
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/pesquisa_precos   # .env
uv run alembic upgrade head        # cria/atualiza o schema
uv run python -m migracao          # lista os 17 passos e o estado de cada um
uv run python -m migracao.m04_catalogo     # um por vez, com pg_dump entre agregados
uv run python -m migracao.validar          # contagens + integridade referencial
```

**O acervo real ainda não foi migrado.** O banco `pesquisa_precos` existe com o schema aplicado
e ZERO linhas. A mecânica dos 17 passos foi validada ponta a ponta num banco descartável, com
uma amostra coerente de 60 mil itens (todas as checagens de integridade em zero, e as etapas
7/8 produzindo o mesmo export pelos dois caminhos). Rodar sobre os 1,6 milhão de itens é do
usuário — e cada agregado pede `pg_dump` antes.

Três coisas descobertas rodando a amostra, que já estão tratadas no código:
- **`termo_norm` NÃO dobra acento** (diverge de docs/05_MIGRACAO.md §m05 de propósito): a
  etapa 1 gera o par com/sem acento para todo termo porque a busca do PNCP é sensível a
  acento. Dobrar colapsaria 499 termos em 338. Ver `core.text.normalizar_termo`.
- **Texto de PDF contém bytes NUL**, que `text` do Postgres rejeita — `db.copy.texto_para_pg`
  os remove no m10.
- **`5_pdf_texto.csv` tem cada página 2×** (extração append-only rodou duas vezes, texto
  idêntico). A PK dedupa; a contagem final fica ~metade das 888.656 linhas do CSV. Esperado.

Diferença conhecida no export entre CSV e banco: a coluna `Unidade` perde o espaço à direita
que o PNCP devolve (473 de 8.154 linhas na amostra). Só isso; nenhuma outra célula difere.

## Regra nº 1: quem roda a pipeline é o usuário

**Claude NÃO dispara etapas da pipeline.** Isso não mudou na Fase 13 — mudou só a forma. O
usuário sobe a web (`uv run python -m pesquisa_precos`) e dá play em cada etapa pelo navegador,
acompanhando progresso e log ao vivo; a ideia continua sendo human-in-the-loop.

Na prática, para o Claude: **não subir o servidor e não chamar rota que dispara etapa**
(`POST .../executar`, `.../aprovar`). O papel é explicar o que esperar antes de cada etapa, ler
código e ajudar a debugar quando algo falha, inspecionar resultados depois (SQL/Python
**read-only** é OK), e implementar correções/features pontuais quando pedido.

Livres: `tools/`, consultas de leitura ao banco, `pytest`, `ruff`, e subir a app num
`TestClient` para conferir que uma rota responde (não é execução de etapa).

## Restrição crítica de custo de LLM

**Não há orçamento para o modelo caro.** O provedor `openrouter` está cadastrado com
`inclusionai/ling-2.6-flash` (barato) e é ele que atende `chat`. O modelo caro (era
`OPENAI_MODEL_PASS2`, `qwen3.7-plus`) **não foi semeado de propósito** na Fase 14: cadastrá-lo
criaria um provedor pronto para ser apontado por engano.

Até a Fase 0 isso dependia de lembrar de digitar `--fraco` na **etapa 6c** — sem a flag, ela
caía no modelo caro. **A Fase 1 inverteu** (ADR-004): o barato é o padrão e o caro exige
`forte` explícito. Hoje `forte` é um campo do `Params` da 6c, ou seja, uma caixa no formulário
da web; `fraco` sobrevive como campo sem efeito. **Nunca sugerir marcar `forte` na 6c.**

O teto de custo por run (`teto_custo_usd`) é a segunda rede: ele aborta a etapa de forma limpa
ao ser ultrapassado, e vale a pena preenchê-lo ao criar o run.

## "Regra dos 5" — foi removida intencionalmente

O README/GUIA ainda descrevem uma "regra dos 5" (mínimo/top-5 itens por código de catálogo).
Essa regra **foi desativada a pedido explícito do usuário**. Desde a Fase 14 os dois valores
são `Params` da etapa 7 (antes eram `MIN_ITENS`/`TOP_N` no `.env`), com estes defaults:

```
min_itens = 1   # qualquer código fecha com só 1 item confirmado
top_n     = 0   # SEM TETO — traz TODAS as referências confirmadas não sinalizadas por código
```

Isso está confirmado e é comportamento esperado desde 2026-08. **Não tratar >5 itens por
código como bug ou anomalia** — já aconteceu de investigar isso à toa numa sessão anterior.

## Dependência externa: PDFs antigos ficaram para trás

~90% das linhas de dados herdadas do v2 (colunas `pasta_arquivos`) apontam para caminhos
**absolutos** em `C:\Users\diego\Documents\dev\plaseg\itens-via-script\itens-contratos-atas-v2\
data\arquivos\` (111 GB de PDFs brutos). Essa pasta **não foi movida** para este repo — ficou
intencionalmente no lugar antigo, porque:
- os caminhos absolutos continuam válidos enquanto ela não for movida/renomeada;
- os itens já processados por ela já têm o texto extraído em `data/5_pdf_texto.csv`/
  `5_itens_enriquecidos.csv` — não dependem mais do PDF bruto no dia a dia;
- mover 111 GB e reescrever caminhos em CSVs de centenas de MB era desnecessário e arriscado.

Só os ~30k itens coletados **depois** que a v3 começou usam a pasta própria `data/arquivos/`
(relativa, dentro deste repo). **Se um dia `itens-contratos-atas-v2` for movido/apagado**, os
PDFs antigos ficam inacessíveis para reprocessamento (mas os dados já extraídos continuam
intactos aqui).

## Padrão de atualização incremental (o que diferencia o v3)

O v3 existe para permitir `--atualizar`: reprocessar só o que mudou desde a última coleta, sem
refazer tudo do zero. Peças desse desenho, reaproveitáveis como padrão:

- **Watermark** (etapa 2): `data_atualizacao_pncp` é o campo real que a API do PNCP usa para
  ordenar (desc); ele muda quando um documento é atualizado. `data_publicacao_pncp` é imutável.
  Como o v2 nunca salvou o campo real, ele foi reconstruído de forma conservadora a partir de
  `max(data_publicacao_pncp)` por `(termo, tipo_doc)` — sempre ≤ ao watermark real, então nunca
  pula nada (só re-varre um pouco a mais). Isso foi feito **uma vez só**, via
  `tools/seed_watermark_v2.py`; a partir da primeira `--atualizar` real o mecanismo
  normal já sobrescreve com datas reais. **Não precisa rodar de novo.**
- **Dedup por texto** (etapa 3): classifica só `(descricao, unidade)` únicos e propaga o rótulo
  para todos os `item_key` iguais — corta o volume de chamadas de LLM em ~5x. A etapa 5b **não**
  tem esse dedup (cada item chama o LLM individualmente).
- **Custo vs escopo**: etapas caras (3, 5b, 6b-GPU, 6c) só processam itens **novos**
  (resumíveis por chave). Etapas baratas de agregação (4, 7, 8) sempre recomputam o **corpus
  inteiro** (antigo + novo), porque resultados como "mais barato por código" exigem comparar
  itens novos contra os antigos.
- **Cache de embeddings** (etapa 6a): parquet chaveado por `sha1(texto)`; só texto
  novo/desconhecido vai à GPU. Reruns reaproveitam o cache e ficam bem mais rápidos.
- **Corte em streaming** (etapa 6a): o corte top-K + piso por código é aplicado **durante** a
  geração dos pares (numpy direto nas matrizes de score), nunca depois de materializar o
  produto cartesiano completo em um DataFrame — isso já causou um `MemoryError` real com ~33M
  linhas. Não reintroduzir um `aplicar_corte` pós-hoc que dependa de `groupby().rank()` sobre o
  DataFrame inteiro.
- **Snapshot/delta** (etapa 8, flag `--novos`): compara as chaves do export atual contra
  `data/8_export_snapshot.csv` (o snapshot do último `--novos`) para reportar só o que é novo,
  e sempre avança o snapshot no final. O export completo (sem `--novos`) nunca toca o snapshot.
  **Armadilha conhecida**: a primeira vez que `--novos` roda sem snapshot prévio, TUDO aparece
  como novo (mesmo padrão do watermark). Se isso confundir o usuário, a correção é semear o
  snapshot manualmente a partir do baseline que ele realmente quer usar como "já entregue"
  (ex.: as chaves do último export oficial), não tratar como bug.

## Estado da validação (atualizado em 2026-08-22)

⚠ **A tabela abaixo é histórica: ela descreve o pipeline rodando sobre CSV**, validado
end-to-end pelo usuário script por script, reaproveitando os dados migrados do v2. Esse
caminho não existe mais (Fase 13). O que ela ainda prova é que a **regra de negócio** de cada
etapa está certa — não que o caminho de banco já rodou sobre o acervo real.

**O que falta, e é o próximo passo real:** migrar o acervo (`uv run python -m migracao`, passo
a passo, com `pg_dump` entre agregados; depois `migracao.validar`) e então rodar um ciclo
0a → 8 pela web. Hoje o banco tem o schema e as configurações, mas praticamente nenhum dado de
domínio — os 1,6 milhão de itens seguem só nos CSVs.

| Etapa | Status | Observação |
|---|---|---|
| 0a | ✅ | delta limpo (catálogo estável) |
| 1 | ✅ | formato "termos por item" já é o base, sem custo LLM extra |
| watermark artificial | ✅ | seed único via `seed_watermark_v2.py --com-extra`, não repetir |
| 2 (`--atualizar`) | ✅ | +173k itens; progress bar do resgate de pendentes corrigida |
| 3 (classificar) | ✅ | dedup por texto; bug do `--retry-erros` corrigido |
| 4 (cortar) | ✅ | sem LLM; "regra dos 5" já removida (ver acima) |
| 5a/5b (enriquecer PDF) | ✅ | caminho base (sem alt); ~30k itens, 0 erros |
| 6a (pares+embeddings) | ✅ | bug de `MemoryError` corrigido (corte em streaming) |
| 6b (reranker) | ✅ | GPU remota |
| 6c (LLM ambíguos) | ✅ | modelo barato é o padrão (ADR-004); nunca marcar `forte` |
| 7 (agrupar) | ✅ | sem LLM, recomputa tudo |
| 8 (exportar) | ✅ | feature nova `--novos` (delta incremental) implementada e testada |

O caminho de banco (`--fonte banco`, hoje o único) foi validado ponta a ponta num banco
descartável com amostra de 60 mil itens, com o export saindo idêntico pelos dois caminhos —
mas nunca sobre os 1,6 milhão reais.

**`pytest` está verde** (375 passed, 3 skipped em 2026-08-22), assim como
`ruff check pesquisa_precos migracao tools`. Se aparecerem erros de coluna ou tabela
inexistente, falta `alembic upgrade head`.

## Onde ficam as coisas úteis para debugar

- `tools/` — scripts de apoio pontuais (correção de schema, seed de watermark,
  calibração de thresholds). Não fazem parte do fluxo normal.
- `tests/` — guardas estruturais + regra de negócio (`pytest` roda em segundos; os testes de
  banco pulam sozinhos sem Postgres).
- **Estado de execução vive no banco**, não em `data/`: `run`/`run_etapa` (progresso, custo,
  status), `run_log` (log estruturado) e `erro_item` (falha por item, que não derruba a etapa).
  `data/checkpoints/` e `data/erros/` são do pipeline antigo.
- **A tela `/providers`** é o primeiro lugar a olhar quando uma etapa reprova antes de
  começar — e desde a Fase 14 é também onde se conserta. Ela sonda cada capacidade e faz o CRUD
  de provedor. Linha vermelha costuma ser um serviço de `pncp-servicos-locais` fora do ar, ou
  a URL do túnel ngrok da GPU que mudou (agora se troca ali, sem editar arquivo nem reiniciar).
- `.env` — nunca commitar (está no `.gitignore`). Desde a Fase 14 não tem mais chave de API nem
  URL de serviço: só `DATABASE_URL`, `APP_SECRET_KEY` e as credenciais do Resend.
- `legado/` **saiu do repositório** na Fase 0. O patch aposentado
  `2b_corrigir_precos_homologados.py` está na tag `legado-2b-precos-homologados`:
  `git show legado-2b-precos-homologados:legado/2b_corrigir_precos_homologados.py`.
  (O CSV de 41 MB que morava junto nunca esteve no git e continua no disco.)

## Rollback: o repositório congelado

`../pipeline-csv-congelado/` é o pipeline CSV-only no commit anterior a toda a refatoração
(`8f0279c`, preservado com hash e data originais, tag `v3-arquivos`), com os **20,87 GB de
`data/` copiados e verificados** — 9.043 arquivos, checksums conferidos. É autossuficiente:
dá para rodar a pipeline inteira de lá.

Desde a Fase 13 ele é a ÚNICA forma de voltar ao caminho de arquivos — não existe mais flag.
Atenção ao usá-lo: o `CLAUDE.md` que vive lá é o histórico, descreve a "regra dos 5" como ativa
e é anterior à inversão `--fraco`/`--forte`, ou seja, **a 6c de lá cai no modelo caro por
padrão**.

## Dívida conhecida da Fase 0

- ~~`ruff check pesquisa_precos` reporta ~9 achados cosméticos pré-existentes~~ — zerados na
  Fase 1, que reescreveu o corpo das etapas de qualquer forma. `E501`/`E741`/`E702`/`B905`
  seguem desligados no `pyproject.toml`; reativar por módulo quando houver motivo.
- `I001` (ordenação de import) fica desligado **permanentemente** nos módulos de etapa: elas
  fazem `sys.stdout.reconfigure(encoding="utf-8")` antes de importar `rich`/`pandas`, e o
  autofix do isort moveria os imports para cima disso — reintroduzindo o bug de acento
  corrompido no console do Windows.
- `requirements.txt` está obsoleto; a lista canônica é o `[project]` do `pyproject.toml`.
