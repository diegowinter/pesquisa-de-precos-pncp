# Guia de implementação — Pipeline `itens-contratos-atas` v2

> ## ⚠ Leia isto antes de seguir qualquer instrução deste guia
>
> **Este documento é a referência de REGRA DE NEGÓCIO, não de mecânica.** Ele foi escrito
> quando a pipeline era um conjunto de scripts que gravavam CSV em `data/`. Desde a Fase 13
> (ADR-020) nada disso é verdade: não há scripts numerados, não há CLI, nenhuma etapa escreve
> arquivo, e o sistema é operado pela web. Ver [README.md](README.md) e [docs/](docs/).
>
> **O que continua valendo** (e é o motivo de este arquivo existir): as regras de cada etapa —
> como montar `item_key`, o que conta como valor homologado, por que o preço do PDF vence o da
> API, as heurísticas de extração, os limiares, e a lista de armadilhas conhecidas. Essas
> regras foram transportadas para o código intactas.
>
> **O que NÃO vale mais, onde quer que apareça abaixo:**
>
> | O guia diz | Hoje é |
> |---|---|
> | `data/{N}_*.csv` como saída de etapa | uma tabela no Postgres (ver [docs/02_SCHEMA.md](docs/02_SCHEMA.md)) |
> | `python -m pesquisa_precos.etapas.eN_*` | executar a etapa pela web |
> | caminhos em `config/paths.py` | `paths.py` é só do importador `migracao/` |
> | `pesquisa_precos/etapas/` | `pesquisa_precos/steps/` |
> | `executar()` / `estimar()` / `CHAVE` / `VERSAO_CODIGO` | `run()` / `estimate()` / `KEY` / `CODE_VERSION` |
> | `ferramentas/` | `tools/` |
> | tabelas `provedor`, `run_etapa`, `llm_chamada`... | `provider`, `run_step`, `llm_call` (ver a migration 0011) |
> | checkpoint em `data/checkpoints/` | derivado do próprio dado (`par.score_rerank IS NULL`) |
> | erros em `data/erros/{N}_erros.csv` | tabela `erro_item` |
> | `data/erros`, `--dry-run`, `--limite N` como flags | campos do `Params`, no formulário |
> | etapas `5a`/`5b` e `5_alt_*` separadas | uma etapa `5` com estratégias plugáveis (ADR-010) |
> | allow-list em `core/catalogo/local.py` | dado editável: `pdm_permitido`, `grupo_permitido` |
> | "regra dos 5" / top 5 por código | **desativada** (`min_itens=1`, `top_n=0`, ADR-016) |
>
> Quando este guia e o código divergirem sobre MECÂNICA, o código está certo. Quando
> divergirem sobre REGRA, é provável que o guia esteja certo e valha investigar.


Guia para implementação completa da nova pipeline de pesquisa de preços de itens de segurança pública via PNCP. Este documento é autossuficiente: descreve cada script a criar, cada módulo auxiliar, entradas, saídas, convenções e regras de negócio. **Não reescreva do zero o que já existe e funciona** — a seção "Migração do código existente" mapeia o que reaproveitar.

> **Legenda de status** (atualizada em 2026-07-09):
> - ✅ **Feito** — implementado e funcionando.
> - 🟡 **Parcial** — existe, mas ainda não cobre tudo que o guia pede.
> - ⬜ **Pendente** — ainda não desenvolvido.

> **Mudança de rumo (2026-07-09): curadoria por LLM removida.** A seleção do catálogo deixou de ser feita pela Etapa 0b (curadoria em 2 rounds de LLM) e passou a ser um **filtro por allow-list** aplicado dentro da 0a: materiais por `codigoPdm`, serviços por `codigoServico` (constantes em `core/catalogo/local.py`). A saída curada `data/0b_catalogo_curado.csv` foi substituída por `data/0a_catalogo_filtrado.csv`, e todas as etapas que a consumiam foram reapontadas. A Etapa 0b e o script `0b_curar_catalogo.py` foram aposentados.

---

## 1. Convenções obrigatórias

### 1.1 Numeração de scripts e saídas

> **Atualizado pela Fase 0** (ver [docs/04_FASES.md](docs/04_FASES.md)). Os nomes de arquivo
> de saída em `data/` **não mudaram** — o que mudou foi onde o código mora. Este documento
> segue usando os nomes antigos dos scripts nas seções históricas (§5); nas seções normativas
> abaixo, o nome do módulo novo vem entre parênteses.

- Etapas ficam em `pesquisa_precos/steps/`, nomeadas `e{N}{letra?}_{acao}.py` (ex.: `e0a_catalogo.py`, `e6a_pairs.py`), e são executadas pela web — não há entrypoint de linha de comando.
- **Toda saída de dados leva o prefixo da etapa que a produziu**: `data/{N}{letra?}_{descricao}.{csv|parquet|xlsx}`. Ex.: `e2_collect` → `data/2_itens_coletados.csv`. Isso é regra dura: olhar o nome do arquivo deve dizer imediatamente quem o gerou e quem o consome (a etapa seguinte).
- Checkpoints/caches internos (não são saída de etapa) ficam em `data/checkpoints/{N}_...`.
- Erro por item vai para a tabela `item_error`, via `ctx.erro_item(...)`.
- **Nenhum caminho de `data/` é escrito à mão**: todos vivem em `pesquisa_precos/config/paths.py`. Um caminho divergente não levanta erro — a etapa só perde o checkpoint e repaga o LLM.
- Módulos auxiliares (bibliotecas, sem `__main__` de pipeline) ficam em `pesquisa_precos/core/` (regras, io, coleta, prompts) ou `pesquisa_precos/providers/` (LLM, embedder, reranker, OCR). **Nenhum módulo de etapa pode ser importado por outro** — se duas etapas precisam da mesma função, ela vai para `core/`.

### 1.2 Resumibilidade (obrigatória em toda etapa)

Toda etapa que itera sobre registros segue o mesmo padrão:

1. Define uma **chave natural** por registro (documentada na seção da etapa).
2. Escreve resultados incrementalmente com `core/io_seguro.EscritorSeguro` (append + fsync por linha, crash-safe).
3. No início da execução, carrega as chaves já concluídas da própria saída e as pula.
4. Falhas de registro individual vão para `data/erros/{N}_erros.csv` com a chave, o erro e timestamp — e **não derrubam a execução**. Flag `--retry-erros` reprocessa só as chaves do log de erros.

### 1.3 Encoding

Todo I/O de texto usa `encoding="utf-8"` explícito (leitura e escrita, CSV e JSON). O bug de acentos corrompidos (`SERVI��OS`) do código antigo veio de encoding implícito no Windows (cp1252). `EscritorSeguro` deve fixar utf-8 internamente. CSVs lidos com `pd.read_csv(..., encoding="utf-8")`.

### 1.4 LLM e modelos locais

- Toda chamada de LLM (paga ou local via LM Studio) passa por `providers/llm_curador.py`. Nenhum script chama API de LLM diretamente.
- Embeddings e reranking **não** passam pelo `llm_curador`: usam `providers/embedder_local.py` e `providers/reranker_local.py` (sentence-transformers em processo).
- OCR usa `providers/ocr_pdf.py` (chama o servidor de OCR local via HTTP OpenAI-compatible).
- Modelos de GPU (embedder, reranker, OCR, LLM local) **nunca rodam simultaneamente** — a GPU tem 6GB. As etapas são sequenciais, então cada script carrega seu modelo no início e libera ao final. Documentar isso no README.

### 1.5 Configuração (`.env`)

```
# OpenRouter (pago)
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_MODEL_PASS1=...        # barato: classificação, extração PDF
OPENAI_MODEL_PASS2=...        # forte: confirmação de curadoria, validação de pares

# LM Studio (local, OpenAI-compatible)
LOCAL_BASE_URL=http://localhost:1234/v1
LOCAL_MODEL=...               # modelo pequeno p/ classificação (etapas 0b-pass1 e 3)
LOCAL_API_KEY=lm-studio       # dummy

# OCR local (servidor OpenAI-compatible, ver doc de recursos)
OCR_BASE_URL=http://localhost:8000/v1
OCR_MODEL=...

# Modelos em processo (sentence-transformers)
EMBEDDER_MODEL=BAAI/bge-m3
RERANKER_MODEL=BAAI/bge-reranker-v2-m3

# Thresholds (calibráveis)
REJEITOR_THRESHOLD=0.30       # 6a: cosseno mínimo p/ par sobreviver (calibrar p/ recall ~99%)
RERANK_T_ACEITA=0.80          # 6b
RERANK_T_REJEITA=0.30         # 6b
MIN_ITENS=5                   # regra dos 5
TOP_N=5
```

Flags de roteamento por etapa (CLI, não .env): `--provedor local|openrouter` nas etapas que aceitam ambos (0b pass1, 3), default `local`.

---

## 2. Visão geral do fluxo

```
[0a] Obter catálogo + filtro por allow-list (PDM / codigoServico)  ✅
                              │
                              ▼
[1] Conceitos + termos de busca (LLM + edição manual, versionado)  ✅
                              │
                              ▼
[2] Coleta larga PNCP (busca por termo, dedup por doc, download de PDFs)  ✅
                              │
                              ▼
[3] Classificação de categoria por item PNCP (LLM local, multi-label, O(itens))  ✅
                              │
                              ▼
[4] Corte antecipado: descarta categorias com <5 candidatos (pandas puro)  ✅
                              │
                              ▼
[5] Enriquecimento via PDF (parse PyMuPDF → OCR se escaneado → extração guiada LLM → validação âncora)  ✅
                              │
                              ▼
[6a] Gerar pares (catálogo × item, mesma categoria) + rejeitor híbrido BM25+embedding  ✅
[6b] Reranker local (cross-encoder) → aceito / rejeitado / ambíguo  ✅
[6c] LLM cara só nos ambíguos + acúmulo de rótulos  ✅
                              │
                              ▼
[7] Agrupar por código, sanity de preço, regra dos 5 confirmados, top 5 mais baratos  ✅
                              │
                              ▼
[8] Export XLSX Plaseg  ✅
```

Regra de negócio central (**regra dos 5**): a pesquisa de preço exige **5 itens confirmados** por código de catálogo; ficam os 5 mais baratos por preço unitário. Grupos que não fecham 5 confirmados são descartados. O corte da etapa 4 é apenas a versão "matematicamente segura" (impossível chegar a 5 confirmados com <5 coletados); a contagem final e definitiva é na etapa 7, **sobre confirmados**.

Nunca deduplicar pares: se um item do PNCP é candidato a dois códigos de catálogo, as duas combinações são julgadas independentemente.

---

## 3. Etapas em detalhe

### Etapa 0a — `etapas/e0a_catalogo.py` ✅

Renomeação/adaptação do `obter_catalogo.py` atual. Baixa CATMAT (materiais) e CATSER (serviços) completos do Compras.gov.br Dados Abertos, paginado e resumível, **e aplica o filtro por allow-list** que substitui a curadoria por LLM (antiga Etapa 0b).

- **Entrada**: CLI `--tipo material|servico` (default: ambos), `--so-grupos-seguranca`, `--forcar`.
- **Saídas**:
  - `data/0a_catalogo_materiais.parquet`, `data/0a_catalogo_servicos.parquet` (catálogo completo baixado).
  - `data/0a_catalogo_filtrado.csv` — materiais e serviços da allow-list, colunas unificadas: `tipo` (material|servico), `codigo`, `codigo_pdm`, `nome_pdm`, `descricao`, `codigo_grupo`, `nome_grupo`, `nome_classe`. **Esta é a saída que alimenta a Etapa 1** (substitui `0b_catalogo_curado.csv`).
- **Filtro (allow-list)**: constantes em `core/catalogo/local.py` — `PDMS_MATERIAIS` (filtra materiais por `codigoPdm`) e `CODIGOS_SERVICOS` (filtra serviços por `codigoServico`); função `filtrar_curado(tipo, df)`. Editar essas constantes é como se ajusta o escopo do catálogo (antes era curadoria por LLM).
- **LLM**: não.

### Etapa 0b — ~~`0b_curar_catalogo.py`~~ (removida) ❌

**Aposentada.** A curadoria do catálogo por LLM (2 rounds) foi substituída pelo filtro por allow-list embutido na Etapa 0a. O script `0b_curar_catalogo.py` foi removido; a saída `data/0b_catalogo_curado.csv` deixou de existir e foi substituída por `data/0a_catalogo_filtrado.csv`. A classe `Curador`/`llm_curador.py` continua existindo para os métodos de LLM das etapas 3/5/6c (classificação, extração, comparação), apenas sem a orquestração de curadoria.

### Etapa 1 — `etapas/e1_termos.py` ✅

A busca do PNCP é substring burra e sem OR; a estratégia é **gerar termos genéricos direto de cada item** e empurrar a precisão para o funil abaixo. (A abordagem anterior — agrupar em "conceitos" pelo núcleo e gerar sinônimos a partir do conceito — foi abandonada: um conceito genérico como "automóvel/viatura" gerava termos de objetos diferentes, criando trabalho de LLM à frente para descartar.)

Comportamento:

1. **Por item** (LLM, paralelo com cliente por thread), duas chamadas: `gerar_termos_item` (termos genéricos, 1–2 palavras, direto de `nome_pdm`+`descricao`, sem atributos como cor/calibre/motor) e `classificar_categoria`. Chave de resumo: `(tipo, codigo)`. Checkpoint: `data/checkpoints/1_termos_item.csv` (`tipo, codigo, termos, categoria`).
2. **Categoria nunca vazia**: LLM → se vazia, maioria da categoria dentro do mesmo `codigo_pdm` → mapa grosseiro `nome_grupo → categoria` (`core/classificacao/variacoes.py`) → `outros`.
3. **Variações de grafia** (`core/classificacao/variacoes.py`, definidas à mão): se qualquer forma de um grupo aparecer nos termos do item, todas entram (pick-up/pickup/picape; rádio/transceptor/ht; colete balístico/à prova de bala…). Cada termo também é duplicado na forma sem acento.
4. **Agrega um termo por linha**: `termo → união dos `codigos_catalogo``; a categoria da linha é a maioria entre os códigos daquele termo. Grava `data/1_conceitos_termos.csv` (`conceito`=termo, `categoria`, `termos`=termo, `codigos_catalogo`, `origem` `llm|manual`) e `data/1_categoria_por_codigo.csv` (`tipo, codigo, categoria` — fonte canônica por-item consumida pela Etapa 6a).

**Regra de preservação de edição manual**: `1_conceitos_termos.csv` é editável à mão e versionado. Linhas com `origem=manual` são preservadas; as `origem=llm` são reconstruídas a cada rodada a partir do checkpoint. Flag `--regerar` recria do zero (com confirmação interativa). Flags: `--provedor`, `--forte`, `--limite`, `--concurrency`.

- **Entrada**: `data/0a_catalogo_filtrado.csv`.
- **Saída**: `data/1_conceitos_termos.csv` (termos) + `data/1_categoria_por_codigo.csv` (categoria por item).
- **LLM**: sim (2 chamadas por item — termos + categoria; volume baixo).

### Etapa 2 — `etapas/e2_collect.py` ✅

Sucessor do par `1_obter_itens.py` + `1_obter_itens_catalogo.py`. **Reaproveita** as libs `core/coleta/search_pncp.py`, `core/coleta/fetch_files.py`, `core/coleta/fetch_items.py` — a lógica de busca/paginação/filtro homologado do `1_obter_itens.py` atual migra para uma lib `core/coleta/collect_pncp.py` (funções puras, sem `__main__`), eliminando o hack de `importlib` para módulo que começa com dígito.

Comportamento, para cada `termo` de cada `conceito` de `data/1_conceitos_termos.csv`, para cada tipo de documento (contrato, ata):

1. Busca paginada no PNCP pelo termo.
2. Filtro: `situacaoCompraItem == 2 (Homologado) AND temResultado == true` + filtro de tipo de arquivo (mesmo do código atual).
3. **Dedup de documento**: cache `data/checkpoints/2_doc_cache.jsonl` keyed por `numeroControlePNCP`. Se o documento já foi visto (mesmo por outro termo), NÃO rebaixa nem re-explode — apenas **acrescenta o conceito atual à lista `conceitos_origem` do item já gravado** (ver formato abaixo). A relação termo→documento é muitos-para-muitos e precisa ser preservada.
4. Documento novo: baixa PDFs para `data/arquivos/<TIPO>_<NUMEROCONTROLE>/` (estrutura atual mantida), consulta itens da compra, explode 1 linha por item.

- **Entrada**: `data/1_conceitos_termos.csv`; CLI `--conceitos` (filtro), `--ignorar-cache`.
- **Saída**: `data/2_itens_coletados.csv`. Colunas mínimas: `item_key` (= `numeroControlePNCP + "::" + numeroItem`, chave universal do item daqui pra frente), `tipo_doc` (contrato|ata), `numeroControlePNCP`, `numeroItem`, `descricao_api`, `unidade`, `quantidade`, `preco_unitario`, `orgao`, `uf`, `data`, `conceitos_origem` (pipe-separated), `pasta_arquivos`.
- **Como atualizar `conceitos_origem` de linha já escrita mantendo append-only**: não reescrever o CSV principal; gravar os acréscimos em `data/checkpoints/2_conceitos_extra.csv` (`item_key, conceito`) e fazer o merge (groupby + união de conceitos) em memória na leitura — fornecer a função `carregar_itens_coletados()` em `core/coleta/collect_pncp.py` que já devolve o DataFrame consolidado. Todas as etapas seguintes leem por essa função, nunca o CSV cru.
- **Chave de resumo**: `(termo, tipo_doc, pagina)` em `data/checkpoints/2_progresso.csv`.
- **LLM**: não.

### Etapa 3 — `etapas/e3_classify.py` ✅

Classificação de categoria por item do PNCP — O(itens), cada item processado **uma única vez** independentemente de quantos termos/conceitos o trouxeram.

Para cada `item_key` único de `2_itens_coletados`:

1. Prompt de classificação (via `llm_curador.classificar_categoria`): recebe `descricao_api` + unidade + as definições de categoria de `core/classificacao/categorias.py` (nome + descrição curta + exemplos positivos e negativos). Pede resposta JSON: `{"categorias": ["viatura"], "confianca": "alta|media|baixa"}` — **multi-label permitido** (item ambíguo entra em 2+ categorias e será pareado em todas) e lista vazia = **nenhuma** (o item morre aqui; a "portaria de nomeação" nunca mais custa nada).
2. Provedor default: `local` (LM Studio). `--provedor openrouter` como alternativa.
3. O prompt deve incluir 4-6 exemplos few-shot fixos em `core/prompts.py`, incluindo obrigatoriamente um caso de armadilha lexical (ex.: "PORTARIA Nº 123 - AQUISIÇÃO DE COMPUTADORES" → nenhuma, mesmo que o termo de busca tenha sido "porta").

- **Entrada**: `data/2_itens_coletados.csv` (via `carregar_itens_coletados()`).
- **Saída**: `data/3_itens_classificados.csv` (`item_key, categorias` pipe-separated ou vazio, `confianca`). Erros: `data/erros/3_erros.csv`.
- **Chave de resumo**: `item_key`.
- **Paralelismo**: mesmo pool de `core/paralelo.py`; com LM Studio local, concorrência baixa (2-4) para não estourar a fila do servidor.

### Etapa 4 — `etapas/e4_cut.py` ✅

Pandas puro, sem API/LLM, rápido. Aplica a versão antecipada e segura da regra dos 5:

1. Junta `3_itens_classificados` (só itens com ≥1 categoria) com os itens coletados.
2. Explode multi-label: 1 linha por `(item_key, categoria)`.
3. Conta itens por categoria. Categoria com `< MIN_ITENS` coletados: **impossível** fechar 5 confirmados → descarta em bloco.
4. **Não** aplica corte por código de catálogo aqui (um código com poucos candidatos ainda pode fechar 5 se a categoria for grande — o pareamento decide).

- **Entrada**: `data/2_itens_coletados.csv`, `data/3_itens_classificados.csv`.
- **Saídas**: `data/4_itens_sobreviventes.csv` (mesmas colunas da etapa 2 + `categorias`), `data/4_relatorio_corte.csv` (`categoria, n_itens_coletados, mantida (bool)`) — este relatório é diagnóstico: mostra onde a coleta está fraca e orienta ajuste de termos na etapa 1.
- **LLM**: não. Não precisa ser resumível (roda em segundos).

### Etapa 5 — `etapas/e5a_ocr.py` + `etapas/e5b_extrair.py` ✅

> Implementada como **dois** módulos (OCR/parse e extração guiada), não um. O caminho
> alternativo por modelo de visão é `e5_alt_a_tabela.py` + `e5_alt_b_casar.py`.

Recicla e integra o ramal órfão (`parsear_pdfs.py` + `extrair_itens_ata.py`), agora **só nos sobreviventes** da etapa 4 e com validação âncora. Motivação: a descrição do item na API é pobre; no PDF é rica — e a descrição rica melhora o reranker (6b), a LLM (6c) e o export final (8).

Sub-fases (dentro do mesmo script, checkpoints separados):

**5.1 Parse + roteamento OCR** (lib `providers/ocr_pdf.py`):
- Para cada documento com ≥1 item sobrevivente: abre cada PDF da pasta com PyMuPDF.
- Por página: extrai texto nativo; calcula densidade (`len(texto_limpo) / n_paginas`... na prática: caracteres por página). Página com `< 100` caracteres extraídos → marcada **escaneada**.
- Página escaneada: rasterizar a **200 DPI** (`page.get_pixmap`), enviar **uma imagem por chamada** ao servidor de OCR (`OCR_BASE_URL`, payload OpenAI-compatible com imagem base64, prompt de conversão para markdown). Nunca enviar o documento inteiro — é isso que estourava o contexto no uso anterior.
- Concatena texto (nativo + OCR) por página, na ordem.
- Checkpoint: `data/checkpoints/5_pdf_texto.parquet` (`doc_key, arquivo, pagina, fonte (nativo|ocr), texto`). Chave de resumo: `(doc_key, arquivo, pagina)`.

**5.2 Extração guiada por item** (via `llm_curador.extrair_item_pdf`, provedor `OPENAI_MODEL_PASS1`):
- Para cada item sobrevivente do documento: prompt que recebe o texto do PDF (se muito longo, janelar: localizar primeiro por regex/fuzzy o trecho com o `numeroItem` ou fragmentos da `descricao_api` e enviar só a janela ±3.000 caracteres) e pede **apenas aquele item**: `{"descricao_completa": ..., "preco_unitario": ..., "quantidade": ..., "encontrado": bool}`. Extração guiada, nunca "liste todos os itens" — reduz alucinação drasticamente.
- **Validação âncora (obrigatória)**: aceitar o enriquecimento **somente se** `preco_unitario` extraído bater com o da API (tolerância relativa 1%) **e** `quantidade` bater (exata, ou tolerância 1 unidade para arredondamento). Se não bater ou `encontrado=false`: descarta a extração e o item segue com a descrição da API — nunca fica pior do que hoje.

- **Entrada**: `data/4_itens_sobreviventes.csv`, PDFs em `data/arquivos/`.
- **Saída**: `data/5_itens_enriquecidos.csv` (`item_key, descricao_final, fonte_descricao (api|pdf), preco_valido (bool), paginas_ocr (int)`). Erros: `data/erros/5_erros.csv`.
- **Chave de resumo** (5.2): `item_key`.
- CLI: `--pular-ocr` (processa só PDFs nativos; útil pra rodar rápido antes de subir o servidor de OCR).

### Etapa 6a — `etapas/e6a_pairs.py` ✅

Gera o universo de pares e mata o lixo óbvio de graça.

1. **Geração de pares**: produto `(codigo_catalogo × item_pncp)` **restrito à mesma categoria** — item classificado como `viatura` só pareia com códigos curados de `viatura`. Item multi-label pareia em todas as suas categorias. **Sem dedup de pares** (regra de negócio).
2. **Score léxico**: BM25 (via `rank_bm25`, lib `core/pareamento/indice_lexical.py`) — corpus = descrições finais dos itens PNCP (tokenização: lowercase, sem acento, split alfanumérico); query = nome+descrição do item de catálogo. Guardar score normalizado por categoria (min-max dentro da categoria).
3. **Score semântico**: cosseno entre embeddings bge-m3 (lib `providers/embedder_local.py`). Embeddings computados **uma vez por texto único** e cacheados: `data/checkpoints/6a_emb_catalogo.parquet` e `data/checkpoints/6a_emb_itens.parquet` (chave = hash do texto). Não precisa de banco de vetores: os pares já estão definidos, é cosseno direto par a par (numpy, vetorizado por categoria).
4. **Rejeição conservadora**: par é rejeitado somente se `max(score_bm25_norm, cosseno) < REJEITOR_THRESHOLD`. A lógica é `max`, não média: basta um dos dois sinais dizer "pode ser" para o par sobreviver. O threshold é calibrado para **recall ~99%** na amostra rotulada (ver seção 6) — este estágio existe para matar "portaria de nomeação vs. porta de madeira", não para decidir matches.

- **Entrada**: `data/4_itens_sobreviventes.csv`, `data/5_itens_enriquecidos.csv`, `data/0a_catalogo_filtrado.csv`.
- **Saída**: `data/6a_pares_candidatos.csv` (`par_key` = `codigo::item_key`, `codigo, item_key, categoria, score_bm25, score_cosseno, sobreviveu (bool)`). Manter os rejeitados no arquivo com `sobreviveu=false` (auditoria); etapas seguintes filtram `sobreviveu=true`.
- **LLM**: não. GPU: embedder (sozinho na GPU).

### Etapa 6b — `etapas/e6b_rerank.py` ✅

Cross-encoder local decide a maioria dos pares, custo zero de token.

1. Para cada par sobrevivente de 6a: score do reranker (`providers/reranker_local.py`, `CrossEncoder(RERANKER_MODEL)`, fp16, batch 16-32) sobre o par de textos `(nome + descricao do catálogo, descricao_final do item)`. Truncar cada lado a ~512 tokens.
2. Decisão por threshold: `score >= RERANK_T_ACEITA` → **aceito**; `score <= RERANK_T_REJEITA` → **rejeitado**; entre os dois → **ambíguo** (vai para 6c).

- **Entrada**: `data/6a_pares_candidatos.csv` (+ textos das saídas anteriores).
- **Saída**: `data/6b_pares_rerankeados.csv` (`par_key, score_rerank, decisao (aceito|rejeitado|ambiguo)`).
- **Chave de resumo**: `par_key` (reranker é rápido, mas o volume pode ser grande — manter resumível).
- GPU: reranker (sozinho).

### Etapa 6c — `etapas/e6c_validate.py` ✅

Só a faixa ambígua do reranker chega aqui — tipicamente a minoria dos pares.

1. Para cada par `ambiguo`: `llm_curador.comparar_par` com `OPENAI_MODEL_PASS2` (modelo forte — a qualidade aqui define a qualidade do resultado final; não usar SLM). Prompt recebe nome+descrição do catálogo e a **descrição enriquecida** do item, e pede JSON `{"mesmo_item": "sim|nao", "justificativa": "..."}`. A instrução deve deixar claro o critério: *"mesmo tipo de item para fins de pesquisa de preço — variações de marca/modelo equivalentes contam como sim; itens de natureza ou finalidade distinta contam como não"*.
2. **Acúmulo de rótulos** (importante): toda decisão final — aceites/rejeições do 6b por threshold extremo E os vereditos do 6c — é appendada em `data/6_rotulos_acumulados.csv` (`par_key, texto_catalogo, texto_item, score_rerank, decisao_final, origem (rerank|llm), timestamp`). Este arquivo cresce entre execuções e serve para recalibrar thresholds e, futuramente, fine-tunar o reranker no domínio.

- **Entrada**: `data/6b_pares_rerankeados.csv` (filtro `ambiguo`).
- **Saídas**: `data/6c_pares_validados.csv` (`par_key, mesmo_item, justificativa`), `data/6_rotulos_acumulados.csv` (append).
- **Chave de resumo**: `par_key`. Erros: `data/erros/6c_erros.csv`.

### Etapa 7 — `etapas/e7_group.py` ✅

Pandas puro. Sucessor do `3_agrupar_itens_catalogo.py` atual.

1. **Confirmados** = pares `aceito` do 6b ∪ pares `mesmo_item=sim` do 6c.
2. **Sanity de preço** antes do ranking: por código de catálogo, flag de outlier por IQR sobre `preco_unitario` dos confirmados (`< Q1 - 3*IQR` ou `> Q3 + 3*IQR` → `flag_preco=true`). Itens flagados **não entram no top 5** mas permanecem no arquivo (coluna de flag) para auditoria manual — um erro de unidade (pistola a R$ 3,50) não pode contaminar a pesquisa de preço. Opcional: arquivo `data/config_faixas_preco.csv` (`categoria, preco_min, preco_max`) mantido à mão; se existir, aplica-se também.
3. **Regra dos 5 (definitiva)**: por código, conta confirmados não-flagados; `< MIN_ITENS` → código descartado.
4. Nos códigos que fecham: mantém os `TOP_N` mais baratos por preço unitário.

- **Entrada**: `data/6b_pares_rerankeados.csv`, `data/6c_pares_validados.csv`, `data/5_itens_enriquecidos.csv`, `data/4_itens_sobreviventes.csv`, `data/0a_catalogo_filtrado.csv`.
- **Saída**: `data/7_itens_agrupados.csv` + `data/7_relatorio_grupos.csv` (`codigo, n_confirmados, n_flagados, fechou (bool)`).
- **LLM**: não.

### Etapa 8 — `etapas/e8_export.py` ✅

Sucessor do `4_exportar_para_plaseg.py`. Mesmas 12 colunas fixas, aba "Itens PLASEG", **uma mudança**: nome/descrição do item usa `descricao_final` (a enriquecida do PDF quando `fonte_descricao=pdf`, senão a da API). Tipo/Código Tipo continuam vindo da classificação de catálogo.

- **Entrada**: `data/7_itens_agrupados.csv`.
- **Saída**: `data/8_itens_plaseg.xlsx`.

### Utilitário — `limpar.py` (reescrever) ✅

CLI com escopos explícitos, nunca apaga tudo por default:
- `--etapa N` — apaga saídas e checkpoints da etapa N em diante (respeitando o encadeamento: limpar a 3 invalida 4-8).
- `--arquivos` — esvazia `data/arquivos/` (downloads).
- `--tudo` — tudo exceto `data/0a_*` (inclui o `0a_catalogo_filtrado.csv`), `data/1_conceitos_termos.csv` e `data/6_rotulos_acumulados.csv` (os ativos caros/curados/manuais), com confirmação interativa.

---

## 4. Módulos auxiliares (`pesquisa_precos/core/` e `pesquisa_precos/providers/`)

| Módulo | Status | Conteúdo |
|---|---|---|
| `io_seguro.py` | ✅ existe | `EscritorSeguro` (CSV append, fsync, utf-8 fixo, header automático), `ler_por_codigo`/`ler_chaves_concluidas(path, col_chave) -> set`, helpers de checkpoint. Extraído da curadoria da v1. |
| `paralelo.py` | ✅ existe | Pool de workers com retry/backoff generalizado: `executar_paralelo(itens, fn, concurrency, on_result, on_error)`. |
| `config.py` | ✅ | Carrega as variáveis do `.env` da seção 1.5 com defaults e validação. Ainda mantém helpers de provedor voltados à curadoria (`resolver_provedor`); revisar quando as etapas de LLM (3/5/6c) forem implementadas. |
| `llm_curador.py` | ✅ | Classe `Curador` é o único ponto de chamada de LLM. **Falta**: métodos `classificar_categoria(descricao, unidade) -> dict` (etapa 3), `extrair_item_pdf(janela_texto, item_api) -> dict` (etapa 5), `comparar_par(texto_catalogo, texto_item) -> dict` (etapa 6c). Aposentar o que era de curadoria (`gerar_termo_busca` → etapa 1; `extrair_itens` → `extrair_item_pdf`). Todo método JSON: resposta JSON pura + strip de cercas markdown + retry 1x em `JSONDecodeError`. |
| `prompts.py` | ✅ | **Falta** os prompts novos (classificação de categoria com few-shots e caso-armadilha; extração guiada; comparação de par; núcleo de conceito; sinônimos de conceito). Prompts de curadoria podem ser removidos (curadoria aposentada). |
| `categorias.py` | ✅ | **Falta** por categoria: `descricao_curta`, `exemplos_positivos` (2-3), `exemplos_negativos` (2-3, incluindo armadilhas lexicais). |
| `catalogo_local.py` | ✅ existe | Carga/filtro do catálogo. **Contém a allow-list que substitui a curadoria**: `PDMS_MATERIAIS`, `CODIGOS_SERVICOS`, `filtrar_curado(tipo, df)`. Paths já em `0a_*`. |
| `search_pncp.py` | ✅ existe | Cliente REST de busca, intocado. |
| `fetch_files.py` | ✅ existe | Intocado. |
| `fetch_items.py` | ✅ existe | Intocado. |
| `collect_pncp.py` | ✅ | A lógica de negócio do `1_obter_itens.py` atual (busca→filtro→download→explode) como funções puras + `carregar_itens_coletados()` (merge do CSV principal com `2_conceitos_extra.csv`). Elimina o hack de importlib. |
| `embedder_local.py` | ✅ | Wrapper de `sentence_transformers.SentenceTransformer(EMBEDDER_MODEL)`: `embed_textos(lista, batch) -> np.ndarray` com cache em parquet por hash de texto, device auto (cuda se couber, senão cpu), fp16 em cuda. |
| `reranker_local.py` | ✅ | Wrapper de `sentence_transformers.CrossEncoder(RERANKER_MODEL)`: `score_pares(lista_de_tuplas, batch) -> np.ndarray`, truncamento a 512 tokens por lado, fp16. |
| `indice_lexical.py` | ✅ | BM25 (`rank_bm25.BM25Okapi`) por categoria: `construir(corpus_tokens)`, `pontuar(query_tokens)`, tokenizador compartilhado (lowercase + unidecode + split alfanumérico). |
| `ocr_pdf.py` | ✅ | `extrair_paginas(pdf_path) -> list[{pagina, texto, densidade}]` (PyMuPDF); `pagina_escaneada(densidade) -> bool` (limiar 100 chars); `rasterizar(page, dpi=200) -> bytes PNG`; `ocr_pagina(png_bytes) -> str` (POST OpenAI-compatible em `OCR_BASE_URL` com imagem base64, retry com backoff, timeout generoso). |
| `erros_log.py` | ✅ existe | Intocado. |

---

## 5. Migração do código existente

> **Registro histórico da migração v1 → v2.** Os nomes de destino aqui são os nomes de
> 2026-07; a Fase 0 os moveu para `pesquisa_precos/` (ver §1.1). Preservado como está
> porque descreve o que foi decidido na época, não o estado atual da árvore.

| Arquivo atual | Destino |
|---|---|
| `obter_catalogo.py` | ✅ virou `0a_obter_catalogo.py` (nomes de saída `0a_*` + filtro por allow-list) |
| `curar_catalogo_grupos_paralelo.py` | ❌ curadoria aposentada; `io_seguro.py`/`paralelo.py` já extraídos. A seleção do catálogo agora é a allow-list da 0a (`scripts/catalogo_local.py`) |
| `curar_catalogo.py`, `curar_catalogo_grupos.py` | mover p/ `legado/` (curadoria descontinuada); não deletar até a v2 rodar ponta a ponta |
| `enriquecer_catalogo_grupos.py` | aposentar (→ `legado/`); substituído por `1_gerar_conceitos.py` |
| `1_obter_itens.py` | lógica migra p/ `scripts/collect_pncp.py`; script → `legado/` |
| `1_obter_itens_catalogo.py` | substituído por `2_coletar_pncp.py`; → `legado/` |
| `2_comparar_itens_catalogo.py` | substituído pela cascata 6a/6b/6c; → `legado/` (o prompt de comparação dele é ponto de partida para `comparar_par`) |
| `3_agrupar_itens_catalogo.py` | substituído por `7_agrupar_top5.py` (mesma base + sanity de preço) |
| `4_exportar_para_plaseg.py` | substituído por `8_exportar_plaseg.py` (mesma base + descricao_final) |
| `parsear_pdfs.py`, `extrair_itens_ata.py` | lógica reciclada em `5_enriquecer_pdf.py`/`ocr_pdf.py` (parse resumível e paralelismo aproveitáveis; a extração muda de "liste tudo" para guiada); scripts → `legado/` |
| `limpar.py` | reescrever (seção 3) |
| Dados antigos (`resultado_contratos_atas*.csv`, `catalogo_classificado_p*.csv`, etc.) | mover p/ `data/legado/`; `data/catalogo/*.parquet` → renomear p/ `data/0a_*.parquet`. `catalogo_curado_grupos.csv` não é mais insumo (curadoria aposentada) |

O README deve ser reescrito ao final refletindo exclusivamente o fluxo v2 (diagrama da seção 2 + tabela script→entrada→saída), com uma seção "Legado" apontando para a pasta.

---

## 6. Calibração de thresholds (tarefa do operador, mas o agente prepara a ferramenta)

Criar `tools/calibrate_thresholds.py`:

1. Amostra estratificada de ~150-200 pares de `6a_pares_candidatos.csv` (cobrindo faixas de score), exporta `tools/amostra_rotulagem.csv` com coluna `rotulo_humano` vazia para preenchimento manual (sim/nao).
2. Com a amostra preenchida: varre thresholds e reporta, para o rejeitor (6a), o maior `REJEITOR_THRESHOLD` que mantém recall ≥ 99% dos `sim`; para o reranker (6b), a curva precisão/recall por threshold e sugestão de `T_ACEITA` (precisão ≥ 97% nos aceitos) e `T_REJEITA` (recall ≥ 99% preservado acima dele), estimando o % de pares que sobra como ambíguo (= custo de LLM esperado).
3. Enquanto não houver amostra rotulada, os defaults do `.env` valem — e são conservadores de propósito (rejeitor frouxo, faixa ambígua larga). `data/6_rotulos_acumulados.csv` das primeiras execuções reais alimenta recalibrações.

## 7. Ordem de implementação e critérios de aceite

1. ✅ **Fundações**: `io_seguro.py`, `paralelo.py`, `config.py`, `.env.example`, e **0a com filtro por allow-list** (substitui a antiga curadoria 0b). Feito.
2. ✅ **Etapas 3 + 4** sobre os dados já coletados existentes (adaptar leitura do CSV legado como entrada provisória) — maior corte de custo com menor esforço. Depende de estender `llm_curador.classificar_categoria`, `prompts.py` e `categorias.py`. Aceite: relatório da etapa 4 gerado; classificação bate ≥ 90% com uma amostra manual de 30 itens.
3. ✅ **Etapas 6a/6b/6c + 7 + 8** (+ libs `embedder_local`, `reranker_local`, `indice_lexical`). Aceite: pipeline fecha ponta a ponta com os dados legados; nº de chamadas de LLM em 6c ≤ 30% dos pares pós-6a (senão, revisar thresholds default).
4. ✅ **Etapa 5** (PDF/OCR) + lib `ocr_pdf`. Aceite: em 10 documentos de teste (misto nativo/escaneado), extração guiada validada por âncora em ≥ 70% dos itens; zero enriquecimentos com âncora inválida aceitos.
5. ✅ **Etapas 1 + 2** (+ lib `collect_pncp`). Aceite: etapa 1 gera conceitos/termos a partir do `0a_catalogo_filtrado.csv` e preserva edição manual; etapa 2 preserva `conceitos_origem` muitos-para-muitos (testar 2 termos que acham o mesmo documento).
6. ✅ **`limpar.py`, README, calibrador**.

Ao final de cada fase, rodar `python -m pytest` se houver testes; no mínimo, cada script deve ter `--dry-run` ou aceitar subset pequeno (`--limite N`) para validação barata.
