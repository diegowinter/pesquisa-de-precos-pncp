# 02 — Esquema de banco de dados

PostgreSQL 16+. Todo o DDL abaixo é normativo: nomes de tabela, coluna e enum devem ser usados
exatamente como escritos.

## 0. Convenções

- Idioma: **português**, `snake_case`, tabelas no **singular** (`item`, não `itens`).
- Chaves naturais são preservadas (`item_key`, `par_key`, `numero_controle_pncp`) porque já são
  a linguagem do domínio e aparecem nos CSVs herdados. Onde há chave natural estável, ela **é** a
  PK — evita um join a mais em tabelas de milhões de linhas.
- `criado_em` / `atualizado_em` como `timestamptz`, default `now()`.
- Dinheiro: `numeric(18,4)`. **Nunca `float`** para preço.
- Texto livre extenso: `text` (o TOAST do Postgres comprime automaticamente).
- Campos abertos/específicos de estratégia: `jsonb`.

## 1. Dimensionamento (medido no acervo atual, 2026-08-16)

| Tabela | Linhas estimadas | Observação |
|---|---:|---|
| `catalogo_item` | 2.212 | catálogo filtrado (grupos de segurança) |
| `termo` | 499 | termos de busca gerados |
| `termo_codigo` | ~3.000 | N:N termo × código |
| `documento` | 68.163 | documentos PNCP distintos |
| `item` | 1.613.517 | itens da API do PNCP |
| `texto_classificacao` | ~320.000 | textos únicos (dedup ~5x) |
| `item_categoria` | ~400.000 | multi-label explodido |
| `documento_extracao` | 35.552 | docs com texto (hoje 2,6 GB de texto) |
| `item_enriquecido` | 302.514 | itens sobreviventes enriquecidos |
| `par` | ~250.000 | candidatos + rerankeados |
| `rotulo` | 250.085 | rótulos acumulados p/ calibração |
| `grupo_item` | 118.722 | resultado final |
| `embedding_cache` | ~305.000 | itens + códigos, bge-m3 1024d |

**Alerta de tamanho:** o gigante era `documento_pagina.texto` (888.656 linhas, 2,6 GB em CSV).
A tabela foi dropada pela ADR-023 — a etapa 5 não transcreve mais o documento inteiro, guarda só
a tabela de itens. Com ela saiu o único dado do schema que crescia sem limite.

## 2. Tipos enumerados

```sql
CREATE TYPE tipo_catalogo      AS ENUM ('material', 'servico');
CREATE TYPE tipo_documento     AS ENUM ('contrato', 'ata');

CREATE TYPE estado_documento   AS ENUM (
    'descoberto',      -- capa obtida da API, nada baixado
    'fora_de_escopo',  -- nenhum item sobreviveu ao corte da etapa 4
    'baixando',
    'extraido',        -- texto obtido, PDF já descartado
    'ilegivel',        -- nenhuma página produziu texto útil
    'suspeito',        -- texto obtido mas nenhum item confirmou (PDF trocado)
    'erro'
);


CREATE TYPE status_enriquecimento AS ENUM (
    'pdf_ok',                 -- item confirmado, preço do PDF ≈ preço da API
    'pdf_ok_diverge',         -- confirmado, preço difere (estimado vs. registrado)
    'pdf_ok_preco_suspeito',  -- confirmado, preço implausível (provável misparse)
    'pdf_ok_sem_preco',       -- confirmado pela descrição, sem preço legível
    'pdf_ok_sem_ref',         -- confirmado, mas a API não trouxe preço de referência
    'qtd_nao_confere',        -- achou algo, nem qtd nem preço confirmam
    'nao_encontrado',
    'sem_texto',
    'erro'
);

CREATE TYPE destino_item      AS ENUM ('manter', 'revisar', 'descartar');
CREATE TYPE decisao_rerank    AS ENUM ('aceito', 'ambiguo', 'rejeitado');
CREATE TYPE veredito_par      AS ENUM ('sim', 'nao', 'indeterminado');
CREATE TYPE decisao_final_par AS ENUM ('confirmado', 'rejeitado', 'pendente');

CREATE TYPE modo_run     AS ENUM ('assistido', 'sequencial', 'amostra', 'simulacao');
CREATE TYPE status_run   AS ENUM ('aberto', 'concluido', 'abortado');
CREATE TYPE status_etapa AS ENUM (
    'nao_iniciada', 'aguardando_aprovacao', 'executando',
    'concluida', 'desatualizada', 'falhou', 'cancelada', 'pulada'
);
CREATE TYPE acao_execucao AS ENUM ('atualizar', 'retomar', 'refazer');
CREATE TYPE capacidade    AS ENUM ('chat', 'embed', 'rerank', 'ocr');
```

## 3. Catálogo e termos de busca

Origem: `0a_catalogo_filtrado.csv`, `1_conceitos_termos.csv`, `1_categoria_por_codigo.csv`.

```sql
-- Item do catálogo CATMAT/CATSER. PK composta: o código só é único dentro do tipo.
CREATE TABLE catalogo_item (
    tipo          tipo_catalogo NOT NULL,
    codigo        text          NOT NULL,
    codigo_pdm    text,
    nome_pdm      text,
    descricao     text          NOT NULL,
    codigo_grupo  text,
    nome_grupo    text,
    nome_classe   text,
    categoria     text,            -- da etapa 1; fonte canônica p/ o pareamento da 6a
    ativo         boolean       NOT NULL DEFAULT true,   -- false = 'removido' no delta 0a
    criado_em     timestamptz   NOT NULL DEFAULT now(),
    atualizado_em timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (tipo, codigo)
);
CREATE INDEX ix_catalogo_categoria ON catalogo_item (categoria) WHERE ativo;

-- Snapshot histórico do catálogo, p/ detectar delta entre execuções da etapa 0a.
CREATE TABLE catalogo_snapshot (
    id         bigserial PRIMARY KEY,
    capturado_em timestamptz NOT NULL DEFAULT now(),
    tipo       tipo_catalogo NOT NULL,
    codigo     text NOT NULL,
    hash_linha text NOT NULL
);
CREATE INDEX ix_catalogo_snap ON catalogo_snapshot (capturado_em, tipo, codigo);

-- Termo de busca gerado pela etapa 1. Um termo serve a vários códigos (N:N).
CREATE TABLE termo (
    id             bigserial PRIMARY KEY,
    termo          text NOT NULL,
    termo_norm     text NOT NULL,          -- sem acento, minúsculo — chave de dedup
    categoria      text,
    origem         text,                   -- 'llm' | 'variacao' | 'manual'
    ativo          boolean NOT NULL DEFAULT true,
    excluido_por   text,                   -- quem desativou no gate da etapa 1
    excluido_em    timestamptz,
    config_versao_id bigint REFERENCES config_versao(id),
    criado_em      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (termo_norm)
);

CREATE TABLE termo_codigo (
    termo_id bigint        NOT NULL REFERENCES termo(id) ON DELETE CASCADE,
    tipo     tipo_catalogo NOT NULL,
    codigo   text          NOT NULL,
    PRIMARY KEY (termo_id, tipo, codigo),
    FOREIGN KEY (tipo, codigo) REFERENCES catalogo_item(tipo, codigo)
);
CREATE INDEX ix_termo_codigo_cod ON termo_codigo (tipo, codigo);
```

> **Nota sobre `1_conceitos_termos.csv`:** a coluna `conceito` é hoje idêntica a `termos` (uma
> linha por termo). O conceito como entidade separada **não existe mais** — não recriar.

## 4. Descoberta: documentos e itens

Origem: `2_itens_coletados.csv` (1,6M linhas, achatado). Aqui é normalizado em documento + item.

```sql
CREATE TABLE documento (
    numero_controle_pncp text NOT NULL PRIMARY KEY,
    tipo_doc             tipo_documento NOT NULL,
    orgao                text,
    orgao_cnpj           text,
    uf                   text,
    ano                  int,
    data                 date,              -- data de publicação (imutável)
    data_assinatura      date,
    data_fim_vigencia    date,
    data_atualizacao_pncp timestamptz,      -- campo real de ordenação da API (watermark)
    url_pncp             text,              -- para rebaixar sob demanda
    n_paginas            int,
    hash_arquivo         text,              -- sha256 do PDF, p/ detectar substituição
    estado               estado_documento NOT NULL DEFAULT 'descoberto',
    n_itens              int NOT NULL DEFAULT 0,
    n_itens_sobreviventes int NOT NULL DEFAULT 0,
    descoberto_no_run_id bigint REFERENCES run(id),
    criado_em            timestamptz NOT NULL DEFAULT now(),
    atualizado_em        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_documento_estado ON documento (estado);
CREATE INDEX ix_documento_atualizacao ON documento (data_atualizacao_pncp DESC);

-- Quais termos encontraram este documento (substitui a coluna conceitos_origem).
CREATE TABLE documento_termo (
    numero_controle_pncp text   NOT NULL REFERENCES documento(numero_controle_pncp) ON DELETE CASCADE,
    termo_id             bigint NOT NULL REFERENCES termo(id),
    PRIMARY KEY (numero_controle_pncp, termo_id)
);

-- O item pertence à COMPRA, não ao documento (ADR-024). A API do PNCP entrega itens por
-- compra e não tem rota de itens por ata; um pregão gera N atas, cada uma com o que um
-- fornecedor ganhou. Enquanto o item era atributo do documento, os 82 itens de um pregão
-- viravam 82 linhas em CADA uma das 25 atas dele — 8,4x de duplicação no acervo de atas.
-- Em qual documento o item foi de fato achado é RESULTADO da etapa 5, e vive em
-- `item_enriquecido.numero_controle_pncp`.
CREATE TABLE item (
    item_key             text NOT NULL PRIMARY KEY,   -- <compra_key>::<numero_item>
    compra_key           text NOT NULL,   -- SEM FK: compra não é linha em `documento`
    numero_item          int  NOT NULL,
    descricao_api        text NOT NULL,
    unidade              text,
    quantidade           numeric(18,4),
    preco_unitario       numeric(18,4),   -- homologado/registrado, quando a API traz
    preco_estimado       numeric(18,4),
    fornecedor           text,
    data_resultado       date,
    texto_hash           text NOT NULL,   -- sha1(norm(descricao_api)||'|'||norm(unidade))
    sobrevivente         boolean NOT NULL DEFAULT false,   -- resultado da etapa 4
    criado_em            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (compra_key, numero_item)
);
CREATE INDEX ix_item_compra_key  ON item (compra_key);
CREATE INDEX ix_item_texto_hash  ON item (texto_hash);
CREATE INDEX ix_item_sobrevivente ON item (sobrevivente) WHERE sobrevivente;

-- Watermark da coleta incremental, por (termo, tipo_doc).
CREATE TABLE coleta_watermark (
    termo_id   bigint         NOT NULL REFERENCES termo(id) ON DELETE CASCADE,
    tipo_doc   tipo_documento NOT NULL,
    watermark  timestamptz    NOT NULL,
    atualizado_em timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (termo_id, tipo_doc)
);
```

> **`texto_hash` é a peça-chave do dedup.** Ele é o que permite classificar 320k textos únicos em
> vez de 1,6M itens — economia de ~5x em chamadas de LLM. **Deve ser calculado na ingestão**
> (etapa 2), não na hora de classificar.

## 5. Classificação

Origem: `3_itens_classificados.csv`. Note que a tabela cara é chaveada por **texto**, não por item.

```sql
-- Cache de classificação POR TEXTO. Sobrevive entre runs: o dedup deixa de ser
-- intra-execução e passa a ser permanente.
CREATE TABLE texto_classificacao (
    texto_hash     text NOT NULL PRIMARY KEY,
    descricao      text NOT NULL,
    unidade        text,
    categorias     text[] NOT NULL DEFAULT '{}',
    confianca      real,
    prompt_versao_id bigint REFERENCES prompt_versao(id),
    modelo         text NOT NULL,
    provedor       text NOT NULL,
    run_id         bigint REFERENCES run(id),
    criado_em      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_texto_classif_cats ON texto_classificacao USING gin (categorias);

-- Multi-label explodido por item — materializa o join p/ o pareamento da 6a.
CREATE TABLE item_categoria (
    item_key  text NOT NULL REFERENCES item(item_key) ON DELETE CASCADE,
    categoria text NOT NULL,
    PRIMARY KEY (item_key, categoria)
);
CREATE INDEX ix_item_categoria_cat ON item_categoria (categoria);
```

**Por que duas tabelas:** `texto_classificacao` é o ativo caro (uma chamada de LLM por linha).
`item_categoria` é derivada e barata — pode ser recomputada por SQL puro a qualquer momento:

```sql
INSERT INTO item_categoria (item_key, categoria)
SELECT i.item_key, unnest(tc.categorias)
FROM item i JOIN texto_classificacao tc USING (texto_hash)
ON CONFLICT DO NOTHING;
```

Se um dia o prompt de classificação mudar, apagar `texto_classificacao` das versões antigas e
reclassificar é uma operação bem delimitada, sem tocar em `item`.

## 6. Extração (etapa 5)

Uma linha por documento, com a tabela de itens em **texto livre** — a saída da 1ª chamada de
LLM ([ADR-023](07_DECISOES.md#adr-023)). Não há coluna `estrategia`: a etapa tem um caminho só.

```sql
-- Uma linha por documento. Reextrair sobrescreve: não existem duas rotas a comparar, e
-- guardar a tabela anterior só deixaria dúvida sobre qual vale.
CREATE TABLE documento_extracao (
    id            bigserial PRIMARY KEY,
    numero_controle_pncp text NOT NULL REFERENCES documento(numero_controle_pncp) ON DELETE CASCADE,
    tabela_texto  text NOT NULL DEFAULT '',   -- a tabela de itens "as it is", como o modelo devolveu
    n_paginas     int,
    tokens_in     bigint NOT NULL DEFAULT 0,
    tokens_out    bigint NOT NULL DEFAULT 0,
    cost_usd      numeric(12,6) NOT NULL DEFAULT 0,
    duration_ms   int,
    model         text,
    provider      text,
    run_id        bigint REFERENCES run(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (numero_controle_pncp)
);

-- ===================================================================
-- CONTRATO DE SAÍDA DA ETAPA 5 — estável, independente de COMO o texto chegou.
-- As etapas 6, 7 e 8 leem SÓ esta tabela, e dela só `descricao_final` e `destino`.
-- Foi o que permitiu trocar a extração inteira (ADR-023) sem tocar em nenhuma delas.
-- ===================================================================
CREATE TABLE item_enriquecido (
    item_key          text NOT NULL REFERENCES item(item_key) ON DELETE CASCADE,
    -- A ata/contrato onde o item foi ENCONTRADO (ADR-024). É aqui que o vínculo
    -- documento<->item nasce: a etapa 5 o descobre lendo a tabela do PDF, porque a API do
    -- PNCP não sabe dizer. Na PK para cobrir o item que aparece em duas atas.
    numero_controle_pncp text NOT NULL
        REFERENCES documento(numero_controle_pncp) ON DELETE CASCADE,
    descricao_final   text NOT NULL,
    fonte_descricao   text NOT NULL,       -- 'pdf' | 'api'
    preco_api         numeric(18,4),
    preco_pdf         numeric(18,4),
    divergencia_preco numeric(10,4),
    fornecedor        text,
    quantidade_pdf    numeric(18,4),
    status            status_enriquecimento NOT NULL,
    destino           destino_item NOT NULL,
    doc_status        estado_documento NOT NULL,
    run_id            bigint REFERENCES run(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_key, numero_controle_pncp)
);
CREATE INDEX ix_enriq_destino ON item_enriquecido (destino);
```

### 6.1 Por que texto livre e não colunas

Cada documento traz as colunas que tem: um traz fornecedor e modelo, outro só
descrição/quantidade/preço. Um esquema fixo obrigaria o modelo a preencher campo inexistente —
convite para inventar. Quem estrutura é a **segunda** chamada, item a item, contra a âncora que
a API já fornece (número do item, descrição, quantidade, preço estimado).

`tabela_texto` vazia é informação, não ausência de dado: significa "este documento foi tentado
e não tem tabela de itens". É o que impede repagar o download na execução seguinte.

### 6.2 Regras que a extração tem de cumprir

Vivem em [`core/extraction.py`](../pesquisa_precos/core/extraction.py), fora da etapa, porque
nunca dependeram de como o texto chegou:

1. **Confirmação por quantidade** (tolerância `max(1.0, 1%)`) como fingerprint anti-PDF-trocado,
   ou match exato de preço acima de `PRECO_FINGERPRINT = 1000.0`.
2. **Banda de sanidade de preço** (`0,3× … 3,0×` do preço da API) para pegar misparse de milhar.
3. **`doc_status`** derivado do documento inteiro (`ok` / `suspeito` / `ilegivel`).
4. **Preço é SAÍDA, não filtro**: confirmado o item, a divergência entre estimado e homologado é
   sinalizada, nunca descartada.

> **`documento_pagina` não existe mais.** Era o gigante do banco (888 mil linhas, 2,6 GB de
> texto por página) e nenhuma etapa a jusante a lia. Dropada pela migração 0012 junto com o
> enum `extraction_strategy` e as colunas `estrategia`/`itens_json`/`n_paginas_ocr`.

## 7. Pareamento (etapas 6a, 6b, 6c)

Decisão: **uma tabela `par`**, não três. `par_key` já é a chave de join entre 6a/6b/6c hoje, e
uma tabela larga com colunas nulas é mais simples de consultar e mais barata de manter que três
tabelas com o mesmo PK.

```sql
CREATE TABLE par (
    par_key       text NOT NULL PRIMARY KEY,
    tipo          tipo_catalogo NOT NULL,
    codigo        text NOT NULL,
    item_key      text NOT NULL REFERENCES item(item_key) ON DELETE CASCADE,
    categoria     text NOT NULL,

    -- 6a
    score_bm25    real,
    score_cosseno real,
    sobreviveu    boolean NOT NULL DEFAULT false,

    -- 6b
    score_rerank  real,
    decisao       decisao_rerank,

    -- 6c (só a faixa ambígua chega aqui)
    veredito      veredito_par,
    justificativa text,
    modelo_6c     text,

    decisao_final decisao_final_par NOT NULL DEFAULT 'pendente',
    run_id        bigint REFERENCES run(id),
    atualizado_em timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tipo, codigo) REFERENCES catalogo_item(tipo, codigo)
);
CREATE INDEX ix_par_item     ON par (item_key);
CREATE INDEX ix_par_codigo   ON par (tipo, codigo) WHERE decisao_final = 'confirmado';
CREATE INDEX ix_par_ambiguo  ON par (decisao) WHERE decisao = 'ambiguo';
CREATE INDEX ix_par_sobrev   ON par (sobreviveu) WHERE sobreviveu;
```

`decisao_final` é derivada e deve ser mantida por trigger ou recomputada ao fim da 6c:
`confirmado` = (`decisao='aceito'`) OU (`veredito='sim'`).

```sql
-- Rótulos acumulados: base de calibração de thresholds e futuro fine-tuning do reranker.
-- Append-only, cresce entre execuções. NUNCA truncar.
CREATE TABLE rotulo (
    id             bigserial PRIMARY KEY,
    par_key        text NOT NULL,
    texto_catalogo text NOT NULL,
    texto_item     text NOT NULL,
    score_rerank   real,
    decisao_final  text NOT NULL,
    origem         text NOT NULL,      -- '6b_threshold' | '6c_llm' | 'humano'
    modelo         text,
    run_id         bigint REFERENCES run(id),
    criado_em      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (par_key, origem)
);
```

```sql
-- Cache de embeddings. A chave INCLUI provedor+modelo+dim: sem isso, trocar de
-- provedor mistura espaços vetoriais silenciosamente (bug muito difícil de achar).
CREATE TABLE embedding_cache (
    texto_hash text NOT NULL,
    provedor   text NOT NULL,
    modelo     text NOT NULL,
    dimensao   int  NOT NULL,
    vetor      bytea NOT NULL,       -- float16 little-endian, dimensao × 2 bytes
    criado_em  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (texto_hash, provedor, modelo, dimensao)
);
```

> `bytea` em vez de `pgvector`: os vetores são carregados em bloco para numpy e o corte top-K é
> feito em memória. Não há busca ANN no banco, então a extensão não pagaria seu custo. Se um dia
> houver busca vetorial em SQL, migrar é uma coluna a mais.

## 8. Resultado (etapas 7 e 8)

```sql
CREATE TABLE grupo_item (
    id            bigserial PRIMARY KEY,
    tipo          tipo_catalogo NOT NULL,
    codigo        text NOT NULL,
    item_key      text NOT NULL REFERENCES item(item_key) ON DELETE CASCADE,
    par_key       text NOT NULL,
    posicao       int  NOT NULL,          -- ranking por preço unitário (1 = mais barato)
    preco_unitario numeric(18,4),
    flag_preco    boolean NOT NULL DEFAULT false,   -- outlier IQR ou fora da faixa
    motivo_flag   text,
    run_id        bigint NOT NULL REFERENCES run(id),
    criado_em     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, tipo, codigo, item_key)
);
CREATE INDEX ix_grupo_codigo ON grupo_item (tipo, codigo, posicao);
CREATE INDEX ix_grupo_run    ON grupo_item (run_id);

-- Faixas de preço por categoria (hoje data/config_faixas_preco.csv).
CREATE TABLE faixa_preco (
    categoria  text PRIMARY KEY,
    preco_min  numeric(18,4),
    preco_max  numeric(18,4),
    config_versao_id bigint REFERENCES config_versao(id)
);

CREATE TABLE export (
    id          bigserial PRIMARY KEY,
    run_id      bigint NOT NULL REFERENCES run(id),
    tipo        text NOT NULL,          -- 'completo' | 'novos'
    arquivo     text NOT NULL,          -- caminho relativo
    n_linhas    int  NOT NULL,
    n_codigos   int  NOT NULL,
    hash_arquivo text,
    criado_em   timestamptz NOT NULL DEFAULT now()
);

-- Snapshot do último export --novos. Serve p/ calcular o delta da próxima vez.
-- ARMADILHA: a primeira execução sem snapshot marca TUDO como novo. Semear a partir
-- do último export oficial em vez de tratar como bug.
CREATE TABLE export_snapshot (
    tipo                 tipo_catalogo NOT NULL,
    codigo               text NOT NULL,
    numero_controle_pncp text NOT NULL,
    numero_item          int  NOT NULL,
    export_id            bigint REFERENCES export(id),
    PRIMARY KEY (tipo, codigo, numero_controle_pncp, numero_item)
);
```

## 9. Execução: runs, etapas, log e custo

```sql
CREATE TABLE run (
    id          bigserial PRIMARY KEY,
    rotulo      text,                    -- nome legível dado pelo usuário
    modo        modo_run NOT NULL DEFAULT 'assistido',
    status      status_run NOT NULL DEFAULT 'aberto',
    config_versao_id bigint NOT NULL REFERENCES config_versao(id),
    teto_custo_usd numeric(12,4),        -- NULL = sem teto (desencorajado)
    custo_usd   numeric(12,6) NOT NULL DEFAULT 0,
    limite_documentos int,               -- só no modo 'amostra'
    criado_por  text,
    criado_em   timestamptz NOT NULL DEFAULT now(),
    concluido_em timestamptz
);

CREATE TABLE run_etapa (
    id            bigserial PRIMARY KEY,
    run_id        bigint NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    etapa         text   NOT NULL,       -- chave do registry: '0a','1','2',...,'8'
    status        status_etapa NOT NULL DEFAULT 'nao_iniciada',
    acao          acao_execucao,         -- qual semântica foi usada no último play
    fingerprint   text,                  -- sha256(versao_codigo || params || deps)

    params_efetivos jsonb NOT NULL DEFAULT '{}',   -- resolvido: default ← config ← override
    params_override jsonb NOT NULL DEFAULT '{}',   -- o que o usuário editou no gate

    total         int,                   -- unidades de trabalho previstas
    processados   int NOT NULL DEFAULT 0,
    erros         int NOT NULL DEFAULT 0,
    heartbeat_em  timestamptz,           -- lease: sem update há > N min = processo morto
    pid           int,
    custo_usd     numeric(12,6) NOT NULL DEFAULT 0,
    metricas      jsonb NOT NULL DEFAULT '{}',     -- resumo pós-etapa mostrado na UI
    mensagem_erro text,

    aprovado_por  text,
    aprovado_em   timestamptz,

    iniciada_em   timestamptz,
    concluida_em  timestamptz,
    UNIQUE (run_id, etapa)
);
CREATE INDEX ix_run_etapa_status ON run_etapa (status);

-- Garante execução única. Adquirido pelo subprocesso, liberado ao terminar.
-- pg_advisory_lock complementa (some sozinho se a conexão cair).
CREATE TABLE execucao_lock (
    id          int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    run_etapa_id bigint REFERENCES run_etapa(id),
    pid         int,
    adquirido_em timestamptz,
    expira_em   timestamptz
);

CREATE TABLE run_log (
    id        bigserial PRIMARY KEY,
    run_id    bigint NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    etapa     text,
    nivel     text NOT NULL,          -- 'debug'|'info'|'warn'|'error'
    mensagem  text NOT NULL,
    contexto  jsonb,
    criado_em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_run_log_run ON run_log (run_id, id DESC);

-- Erros por unidade de trabalho: não derrubam a execução, viram fila de reprocesso.
CREATE TABLE erro_item (
    id        bigserial PRIMARY KEY,
    run_id    bigint NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    etapa     text NOT NULL,
    chave     text NOT NULL,          -- item_key, doc_key, par_key...
    tipo_erro text,
    mensagem  text,
    tentativas int NOT NULL DEFAULT 1,
    resolvido boolean NOT NULL DEFAULT false,
    criado_em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_erro_pendente ON erro_item (etapa, resolvido) WHERE NOT resolvido;

-- Toda chamada a provedor pago. É o que sustenta estimativa, teto e dashboard.
CREATE TABLE llm_chamada (
    id         bigserial PRIMARY KEY,
    run_id     bigint REFERENCES run(id) ON DELETE CASCADE,
    etapa      text,
    capacidade capacidade NOT NULL,
    provedor   text NOT NULL,
    modelo     text NOT NULL,
    prompt_versao_id bigint REFERENCES prompt_versao(id),
    chave      text,                   -- item_key / par_key / doc_key
    tokens_in  int NOT NULL DEFAULT 0,
    tokens_out int NOT NULL DEFAULT 0,
    custo_usd  numeric(12,6) NOT NULL DEFAULT 0,
    duracao_ms int,
    sucesso    boolean NOT NULL DEFAULT true,
    criado_em  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_llm_run_etapa ON llm_chamada (run_id, etapa);
CREATE INDEX ix_llm_data      ON llm_chamada (criado_em);
```

> **Escrita transacional obrigatória:** o resultado de uma unidade de trabalho e o avanço do seu
> estado vão no **mesmo commit**. Se gravar o resultado e falhar antes de marcar como feito,
> a retomada paga o LLM de novo.

## 10. Configuração, prompts e provedores

Princípio: **vai para o banco o que muda a resposta; fica no código o que muda o método.**

| Banco (editável pela interface) | Código (exige PR e review) |
|---|---|
| thresholds, `MIN_ITENS`, `TOP_N`, faixas de preço | parsers da API do PNCP |
| termos de busca (ativar/desativar) | lógica de agrupamento e de menor preço |
| prompts e suas versões | schema das etapas, contrato de saída |
| modelo, provedor, URL da GPU | fórmula de score, algoritmo de corte |
| tetos de custo, batch size | regras de validação de item |

```sql
-- Config é VERSIONADA e IMUTÁVEL: editar cria versão nova; o run aponta para uma versão.
-- Sem isso, "por que o resultado mudou?" fica sem resposta.
CREATE TABLE config_versao (
    id         bigserial PRIMARY KEY,
    rotulo     text,
    criado_por text,
    criado_em  timestamptz NOT NULL DEFAULT now(),
    notas      text
);

CREATE TABLE config_valor (
    config_versao_id bigint NOT NULL REFERENCES config_versao(id) ON DELETE CASCADE,
    chave  text  NOT NULL,          -- 'rejeitor_threshold', 'top_n', 'e5.janela_max', ...
    valor  jsonb NOT NULL,
    PRIMARY KEY (config_versao_id, chave)
);

CREATE TABLE prompt (
    nome      text PRIMARY KEY,     -- 'classificar_item', 'extrair_item_pdf', 'comparar_par', ...
    descricao text,
    capacidade capacidade NOT NULL DEFAULT 'chat'
);

CREATE TABLE prompt_versao (
    id        bigserial PRIMARY KEY,
    prompt_nome text NOT NULL REFERENCES prompt(nome) ON DELETE CASCADE,
    versao    int  NOT NULL,
    template  text NOT NULL,        -- com placeholders {descricao}, {unidade}, ...
    ativa     boolean NOT NULL DEFAULT false,
    criado_por text,
    criado_em timestamptz NOT NULL DEFAULT now(),
    notas     text,
    UNIQUE (prompt_nome, versao)
);
CREATE UNIQUE INDEX ux_prompt_ativa ON prompt_versao (prompt_nome) WHERE ativa;

CREATE TABLE provedor (
    nome       text PRIMARY KEY,        -- 'gpu_caseira', 'openrouter', 'lm_studio'
    capacidades capacidade[] NOT NULL,
    base_url   text NOT NULL,
    api_key_cifrada bytea,              -- F14/ADR-022: AES-GCM; era `api_key_ref text`
    api_key_last4   text,               -- só para a tela exibir `sk-or-…7b9d`
    api_key_key_id  text,               -- qual chave-mestra cifrou (permite rotação)
    modelo_padrao text,
    batch_size int NOT NULL DEFAULT 32,
    rpm_limite int,
    custo_in_por_mtok  numeric(10,4),   -- p/ estimativa e para llm_chamada.custo_usd
    custo_out_por_mtok numeric(10,4),
    ativo      boolean NOT NULL DEFAULT true,
    prioridade int NOT NULL DEFAULT 100,
    permite_fallback boolean NOT NULL DEFAULT false,  -- SEMPRE false p/ 'embed'
    atualizado_em timestamptz NOT NULL DEFAULT now()
);

-- Qual provedor atende cada capacidade. Uma linha por capacidade.
CREATE TABLE capacidade_provedor (
    capacidade capacidade PRIMARY KEY,
    provedor   text NOT NULL REFERENCES provedor(nome),
    modelo     text,
    fallback   text REFERENCES provedor(nome)   -- proibido em 'embed' (ver ADR-006)
);

CREATE TABLE provedor_status (
    provedor    text PRIMARY KEY REFERENCES provedor(nome) ON DELETE CASCADE,
    saudavel    boolean NOT NULL,
    latencia_ms int,
    mensagem    text,
    verificado_em timestamptz NOT NULL DEFAULT now()
);
```

> ⚠ **Revisto na Fase 14 ([ADR-022](07_DECISOES.md#adr-022)).** A regra acima era "chave de API
> nunca vai para o banco": `api_key_ref` guardava o *nome* da env var e o valor ficava no `.env`.
> O efeito colateral foi que cadastrar um provedor pela tela continuava impossível sem editar
> arquivo e reiniciar. A chave passa a morar no banco **cifrada**: `api_key_ref` dá lugar a
> `api_key_cifrada bytea` + `api_key_last4 text` + `api_key_key_id text`, decifradas só em
> processo, por uma chave-mestra (`APP_SECRET_KEY`) que continua fora do banco, no ambiente do
> serviço. A chave nunca volta pela API nem pelo HTML — a tela mostra os 4 últimos dígitos.

## 11. Retenção e limpeza

Desde a ADR-023 nenhuma tabela cresce sem limite: `documento_pagina`, que era o caso, não
existe mais. Sobrou uma retenção só, a do log.

| Dado | Retenção | Justificativa |
|---|---|---|
| `item`, `item_enriquecido`, `par`, `grupo_item` | permanente | é o produto |
| `texto_classificacao`, `embedding_cache`, `rotulo` | permanente | ativo caro, recomprá-lo é perda |
| `documento_extracao.tabela_texto` | permanente | pequeno, e é o insumo direto do enriquecimento |
| `run_log` | 90 dias | diagnóstico, não produto |
| `llm_chamada` | permanente | série histórica de custo |
| PDF bruto | **minutos** | descartado assim que o texto é extraído |

## 12. Consulta de auditoria (teste de fogo do schema)

Este é o requisito nº 4 do projeto. Se esta consulta não for natural, o schema está errado.

```sql
-- De uma linha do export até a origem completa.
SELECT
    g.codigo, g.posicao, g.preco_unitario, g.flag_preco,
    i.item_key, i.numero_item, i.descricao_api,
    ie.descricao_final, ie.fonte_descricao, ie.status,
    d.numero_controle_pncp, d.orgao, d.uf, d.url_pncp, d.estado AS doc_estado,
    p.score_bm25, p.score_cosseno, p.score_rerank, p.decisao, p.veredito,
    tc.categorias, tc.modelo AS modelo_classificacao,
    pv.prompt_nome, pv.versao AS versao_prompt,
    r.id AS run_id, r.rotulo AS run_rotulo
FROM grupo_item g
JOIN item              i  ON i.item_key = g.item_key
JOIN documento         d  ON d.numero_controle_pncp = i.numero_controle_pncp
LEFT JOIN item_enriquecido ie ON ie.item_key = i.item_key
LEFT JOIN par          p  ON p.par_key = g.par_key
LEFT JOIN texto_classificacao tc ON tc.texto_hash = i.texto_hash
LEFT JOIN prompt_versao pv ON pv.id = tc.prompt_versao_id
JOIN run               r  ON r.id = g.run_id
WHERE g.codigo = $1 AND g.run_id = $2
ORDER BY g.posicao;
```

## 13. Ordem de criação (dependências de FK)

Há referências circulares entre `run` e as tabelas de resultado. Resolver assim:

1. Enums
2. `config_versao`, `config_valor`, `prompt`, `prompt_versao`, `provedor`, `capacidade_provedor`
3. `run`, `run_etapa`, `execucao_lock`, `run_log`, `erro_item`, `llm_chamada`
4. `catalogo_item`, `catalogo_snapshot`, `termo`, `termo_codigo`
5. `documento`, `documento_termo`, `item`, `coleta_watermark`
6. `texto_classificacao`, `item_categoria`
7. `documento_extracao`, `item_enriquecido`
8. `par`, `rotulo`, `embedding_cache`
9. `grupo_item`, `faixa_preco`, `export`, `export_snapshot`

A FK `termo.config_versao_id` e `documento.descoberto_no_run_id` são adicionadas via
`ALTER TABLE` depois do passo 3 — ou declaradas `DEFERRABLE`.
