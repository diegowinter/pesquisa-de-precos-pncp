# Pesquisa de preços PLASEG via PNCP

Pesquisa de preços de itens de segurança pública. Parte do catálogo CATMAT/CATSER filtrado por
uma allow-list curada, coleta contratos e atas do PNCP por termo de busca, e afunila com um
funil de custo crescente (classificação → corte → rejeitor híbrido → reranker local → LLM só
no ambíguo) até as referências confirmadas mais baratas por código de catálogo.

**O sistema inteiro é operado pelo navegador.** Não há CLI, não há script para encadear, e
nenhuma etapa escreve arquivo: o PostgreSQL é o único meio de persistência.

```
uv sync
uv run alembic upgrade head          # schema
uv run python -m pesquisa_precos     # http://localhost:8001
```

O desenho de engenharia está em [`docs/`](docs/) — comece por
[`docs/README.md`](docs/README.md). As regras de negócio herdadas (formatos, heurísticas,
armadilhas de cada etapa) estão em
[`GUIA_IMPLEMENTACAO_PIPELINE.md`](GUIA_IMPLEMENTACAO_PIPELINE.md), que descreve o pipeline na
época em que ele gravava CSV — a regra continua valendo, os caminhos de arquivo não.

> **A "regra dos 5" está DESATIVADA** (`min_itens=1`, `top_n=0`). Mais de 5 itens por código é
> comportamento esperado, não bug. Ver [ADR-016](docs/07_DECISOES.md#adr-016).

## Fluxo

Onze etapas, na ordem que `pesquisa_precos/steps/registry.py` declara. Cada uma lê e escreve
no banco; a coluna "produz" é a tabela onde o resultado fica.

| Etapa | O que faz | Produz | Custo |
|---|---|---|---|
| `0a` | baixa CATMAT/CATSER e aplica a allow-list curada | `catalogo_raw`, `catalogo_item` | — |
| `1` | gera os termos de busca por item de catálogo | `termo`, `termo_codigo` | LLM |
| `2` | coleta larga no PNCP (busca → documento → itens) | `documento`, `item` | — |
| `3` | classifica a categoria de cada item | `texto_classificacao`, `item_categoria` | LLM |
| `4` | corta quem não tem categoria de conteúdo | `item.sobrevivente` | — |
| `5` | baixa o PDF; o serviço extrai texto e OCRa | `documento_pagina`, `item_enriquecido` | PDF+OCR+LLM |
| `6a` | pares catálogo × item da mesma categoria + rejeitor | `par` | GPU |
| `6b` | reranker decide aceito / rejeitado / ambíguo | `par.score_rerank` | GPU |
| `6c` | LLM julga só os ambíguos | `par.decisao_final`, `rotulo` | LLM |
| `7` | agrupa por código, sanity de preço, ranking | `grupo_item` | — |
| `8` | monta o XLSX PLASEG | `export.conteudo` | — |

O funil é a razão de ser do desenho: cada etapa é mais cara que a anterior e recebe menos
itens. As etapas caras (3, 5, 6b, 6c) só processam o inédito — o dedup da 3, por exemplo, é
permanente (`texto_classificacao` sobrevive entre runs, e um texto já pago nunca volta ao
modelo). As baratas de agregação (4, 7, 8) recomputam o corpus inteiro, porque "mais barato
por código" exige comparar o novo contra o antigo.

## Como se opera

Tudo pela web, em `localhost:8001`:

- **Runs** — crie um run (com teto de custo, se quiser) e dê play etapa por etapa. O grafo
  mostra o estado de cada uma; a tela da etapa traz progresso ao vivo, log, erros por item, a
  estimativa antes de gastar e o que fica desatualizado se você refizer.
- **Configuração** — os parâmetros de cada etapa, num formulário gerado dos próprios `Params`
  Pydantic. Salvar cria uma `config_versao` nova; runs apontam para uma versão.
- **Prompts** — versionados, com diff e ativação.
- **Provedores** — sonda `chat`/`embed`/`rerank`/`pdf`/`pareamento`. É o primeiro lugar a
  olhar quando uma etapa reprova antes de começar.
- **Custo**, **Exports** (download do XLSX), **Diff entre runs**, **Recalibrar** thresholds.

Há também uma superfície JSON no mesmo processo, sob `/api` (protegida por `X-API-Token`, se
`API_TOKEN` estiver definido). Mesmos serviços, outra representação.

## Invariantes que não se negociam

- **O processo web nunca executa etapa na própria thread** (ADR-002). Dar play grava a intenção
  e sobe `runner/processo.py` como subprocesso, com lock, heartbeat, lease e custo no banco.
  A rota volta na hora.
- **Nenhuma etapa toca em disco** (ADR-018/ADR-020). Não importa `config/paths.py`, não expõe
  `Path`. `tests/test_estrutura.py` guarda isso. A exceção é o PDF em trânsito, que vive numa
  pasta temporária pelo tempo do upload e é apagada no `finally` (ADR-012).
- **Nenhum provedor roda em processo** (ADR-021). Todo adapter em `providers/adaptadores.py` é
  cliente de um serviço; um adapter "em processo" reintroduzido traria torch para o servidor
  que só deveria orquestrar. `tests/test_bloco_d_banco.py` guarda isso.
- **Toda etapa é resumível**, e o checkpoint é derivado do próprio dado (`par.score_rerank IS
  NULL`, `documento.estado`), não de um arquivo à parte. Matar o processo no meio e retomar não
  reprocessa nem duplica.
- **Erro de unidade não derruba a etapa**: vai para `erro_item` e o laço segue. Só falha de
  infraestrutura aborta.
- **GPU (6 GB)**: embedder, reranker, OCR e LLM local nunca rodam ao mesmo tempo. As etapas são
  sequenciais e cada uma carrega o modelo no início e libera no fim.

## Configuração

Copie `.env.example` para `.env`. O mínimo é `DATABASE_URL`; sem banco de pé não há pipeline.

```
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/pesquisa_precos
WEB_PORT=8001        # opcional
WEB_SENHA=           # vazio desliga o login (conveniente local; obrigatório se expuser)
API_TOKEN=           # vazio desliga a checagem das rotas /api
```

Chaves de provedor, modelos e thresholds também vivem no `.env`, mas o que estiver no banco
(`config_valor`, `capacidade_provedor`) tem precedência — é o que a interface edita.

## Os serviços pesados vivem fora daqui

Este processo faz scraping, orquestra e escreve no banco. **Nada de GPU nem de CPU intensiva
roda nele** (ADR-021) — é o que permite hospedá-lo num servidor modesto. PyMuPDF, OCR,
embedder, reranker e BM25 estão no repositório companion
[`../pncp-servicos-locais`](../pncp-servicos-locais), atrás de HTTP:

```
cd ../pncp-servicos-locais
uv sync --extra pdf                # ou gpu / ocr / pareamento
python -m servicos pdf --host 0.0.0.0 --port 8200
```

E aqui, o endereço de cada serviço se cadastra em `/provedores` e vive na tabela
`provider_capability` (ADR-022) — não no `.env`. Capacidade sem provedor apontado não é "roda
aqui": a etapa para antes de começar, dizendo qual falta. O OCR não aparece nesta lista porque
quem o chama é o serviço de `pdf`, na máquina dele.

O companion é independente: não importa nada deste pacote, e os dois repositórios não
precisam ficar no mesmo disco. Rodar tudo na própria máquina é rodar os dois.

O contrato de `pdf` reflete a linha do corte: **este** processo baixa os PDFs (I/O barato, e o
cliente da API do PNCP já existe para a etapa 2) e manda os bytes; o serviço faz o parse, a
rasterização a 200 DPI e o OCR, e devolve texto.

## Estado atual

O acervo real — **1,6 milhão de itens** — ainda não foi migrado: ele vive nos CSVs de `data/`,
e `migracao/` (21 passos, com `COPY`) é o que o leva para o Postgres.

```
uv run python -m migracao                  # lista os passos e o estado de cada um
uv run python -m migracao.m04_catalogo     # um por vez, com pg_dump entre agregados
uv run python -m migracao.validar          # contagens + integridade referencial
```

Enquanto isso não roda, o banco tem schema e configuração, mas quase nenhum dado de domínio.

## Utilitários

- `tools/calibrate_thresholds.py --amostrar | --analisar` — prepara a amostra rotulável e
  sugere `REJEITOR_THRESHOLD`, `RERANK_T_ACEITA`, `RERANK_T_REJEITA`. A tela **Recalibrar** faz
  o mesmo cálculo sobre `rotulo`, sem gravar nada.
- `tools/regressao.py` — precisão/recall dos thresholds vigentes.
- `pytest` — guardas estruturais e de regra de negócio. Os testes de banco pulam sozinhos sem
  Postgres.

## Rollback e legado

`../pipeline-csv-congelado/` guarda o pipeline CSV-only anterior a toda a refatoração, com os
20,87 GB de `data/` verificados. É autossuficiente. Desde a Fase 13 é a única forma de voltar
ao caminho de arquivos — não existe mais flag. Cuidado: o `CLAUDE.md` de lá é histórico, e a
6c daquele código cai no **modelo caro** por padrão.

Os 111 GB de PDFs herdados do v2 continuam em
`../itens-via-script/itens-contratos-atas-v2/data/arquivos/`, referenciados por caminho
absoluto nos dados antigos — ver [`CLAUDE.md`](CLAUDE.md). A curadoria do catálogo por LLM
(antiga etapa 0b) foi aposentada e substituída pela allow-list, que hoje é dado editável
(`pdm_permitido`, `grupo_permitido`), não código.
