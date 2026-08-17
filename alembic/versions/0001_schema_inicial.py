"""schema inicial — DDL de docs/02_SCHEMA.md

Revision ID: 0001
Revises:
Create Date: 2026-08-16

O DDL abaixo é COPIADO de docs/02_SCHEMA.md, que é normativo: nome de tabela, de coluna, de
enum e de índice devem ser exatamente os de lá. Por isso esta migration é SQL literal e não
`op.create_table()` — a tradução para a API do Alembic perderia os índices parciais
(`WHERE ativo`, `WHERE sobreviveu`), o índice GIN de `texto_classificacao.categorias`, a coluna
gerada `documento_pagina.n_chars` e o `UNIQUE ... WHERE ativa` de `prompt_versao`. Todos são
comportamento, não estética.

A ordem dos blocos é a de §13 (dependências de FK). Com ela, nenhuma FK aponta para frente e
não é preciso `ALTER TABLE` de fechamento nem constraint `DEFERRABLE`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── 1. Enums (02_SCHEMA.md §2) ──────────────────────────────────────────────────────
ENUMS = """
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

CREATE TYPE estrategia_extracao AS ENUM ('janela', 'completa', 'visao');

CREATE TYPE status_enriquecimento AS ENUM (
    'pdf_ok',
    'pdf_ok_diverge',
    'pdf_ok_preco_suspeito',
    'pdf_ok_sem_preco',
    'pdf_ok_sem_ref',
    'qtd_nao_confere',
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
"""

# ── 2. Config, prompts e provedores (§10) ───────────────────────────────────────────
CONFIG = """
CREATE TABLE config_versao (
    id         bigserial PRIMARY KEY,
    rotulo     text,
    criado_por text,
    criado_em  timestamptz NOT NULL DEFAULT now(),
    notas      text
);

CREATE TABLE config_valor (
    config_versao_id bigint NOT NULL REFERENCES config_versao(id) ON DELETE CASCADE,
    chave  text  NOT NULL,
    valor  jsonb NOT NULL,
    PRIMARY KEY (config_versao_id, chave)
);

CREATE TABLE prompt (
    nome      text PRIMARY KEY,
    descricao text,
    capacidade capacidade NOT NULL DEFAULT 'chat'
);

CREATE TABLE prompt_versao (
    id        bigserial PRIMARY KEY,
    prompt_nome text NOT NULL REFERENCES prompt(nome) ON DELETE CASCADE,
    versao    int  NOT NULL,
    template  text NOT NULL,
    ativa     boolean NOT NULL DEFAULT false,
    criado_por text,
    criado_em timestamptz NOT NULL DEFAULT now(),
    notas     text,
    UNIQUE (prompt_nome, versao)
);
CREATE UNIQUE INDEX ux_prompt_ativa ON prompt_versao (prompt_nome) WHERE ativa;

CREATE TABLE provedor (
    nome       text PRIMARY KEY,
    capacidades capacidade[] NOT NULL,
    base_url   text NOT NULL,
    api_key_ref text,
    modelo_padrao text,
    batch_size int NOT NULL DEFAULT 32,
    rpm_limite int,
    custo_in_por_mtok  numeric(10,4),
    custo_out_por_mtok numeric(10,4),
    ativo      boolean NOT NULL DEFAULT true,
    prioridade int NOT NULL DEFAULT 100,
    permite_fallback boolean NOT NULL DEFAULT false,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE capacidade_provedor (
    capacidade capacidade PRIMARY KEY,
    provedor   text NOT NULL REFERENCES provedor(nome),
    modelo     text,
    fallback   text REFERENCES provedor(nome)
);

CREATE TABLE provedor_status (
    provedor    text PRIMARY KEY REFERENCES provedor(nome) ON DELETE CASCADE,
    saudavel    boolean NOT NULL,
    latencia_ms int,
    mensagem    text,
    verificado_em timestamptz NOT NULL DEFAULT now()
);
"""

# ── 3. Execução (§9) ────────────────────────────────────────────────────────────────
EXECUCAO = """
CREATE TABLE run (
    id          bigserial PRIMARY KEY,
    rotulo      text,
    modo        modo_run NOT NULL DEFAULT 'assistido',
    status      status_run NOT NULL DEFAULT 'aberto',
    config_versao_id bigint NOT NULL REFERENCES config_versao(id),
    teto_custo_usd numeric(12,4),
    custo_usd   numeric(12,6) NOT NULL DEFAULT 0,
    limite_documentos int,
    criado_por  text,
    criado_em   timestamptz NOT NULL DEFAULT now(),
    concluido_em timestamptz
);

CREATE TABLE run_etapa (
    id            bigserial PRIMARY KEY,
    run_id        bigint NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    etapa         text   NOT NULL,
    status        status_etapa NOT NULL DEFAULT 'nao_iniciada',
    acao          acao_execucao,
    fingerprint   text,
    params_efetivos jsonb NOT NULL DEFAULT '{}',
    params_override jsonb NOT NULL DEFAULT '{}',
    total         int,
    processados   int NOT NULL DEFAULT 0,
    erros         int NOT NULL DEFAULT 0,
    heartbeat_em  timestamptz,
    pid           int,
    custo_usd     numeric(12,6) NOT NULL DEFAULT 0,
    metricas      jsonb NOT NULL DEFAULT '{}',
    mensagem_erro text,
    aprovado_por  text,
    aprovado_em   timestamptz,
    iniciada_em   timestamptz,
    concluida_em  timestamptz,
    UNIQUE (run_id, etapa)
);
CREATE INDEX ix_run_etapa_status ON run_etapa (status);

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
    nivel     text NOT NULL,
    mensagem  text NOT NULL,
    contexto  jsonb,
    criado_em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_run_log_run ON run_log (run_id, id DESC);

CREATE TABLE erro_item (
    id        bigserial PRIMARY KEY,
    run_id    bigint NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    etapa     text NOT NULL,
    chave     text NOT NULL,
    tipo_erro text,
    mensagem  text,
    tentativas int NOT NULL DEFAULT 1,
    resolvido boolean NOT NULL DEFAULT false,
    criado_em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_erro_pendente ON erro_item (etapa, resolvido) WHERE NOT resolvido;

CREATE TABLE llm_chamada (
    id         bigserial PRIMARY KEY,
    run_id     bigint REFERENCES run(id) ON DELETE CASCADE,
    etapa      text,
    capacidade capacidade NOT NULL,
    provedor   text NOT NULL,
    modelo     text NOT NULL,
    prompt_versao_id bigint REFERENCES prompt_versao(id),
    chave      text,
    tokens_in  int NOT NULL DEFAULT 0,
    tokens_out int NOT NULL DEFAULT 0,
    custo_usd  numeric(12,6) NOT NULL DEFAULT 0,
    duracao_ms int,
    sucesso    boolean NOT NULL DEFAULT true,
    criado_em  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_llm_run_etapa ON llm_chamada (run_id, etapa);
CREATE INDEX ix_llm_data      ON llm_chamada (criado_em);
"""

# ── 4. Catálogo e termos (§3) ───────────────────────────────────────────────────────
CATALOGO = """
CREATE TABLE catalogo_item (
    tipo          tipo_catalogo NOT NULL,
    codigo        text          NOT NULL,
    codigo_pdm    text,
    nome_pdm      text,
    descricao     text          NOT NULL,
    codigo_grupo  text,
    nome_grupo    text,
    nome_classe   text,
    categoria     text,
    ativo         boolean       NOT NULL DEFAULT true,
    criado_em     timestamptz   NOT NULL DEFAULT now(),
    atualizado_em timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (tipo, codigo)
);
CREATE INDEX ix_catalogo_categoria ON catalogo_item (categoria) WHERE ativo;

CREATE TABLE catalogo_snapshot (
    id         bigserial PRIMARY KEY,
    capturado_em timestamptz NOT NULL DEFAULT now(),
    tipo       tipo_catalogo NOT NULL,
    codigo     text NOT NULL,
    hash_linha text NOT NULL
);
CREATE INDEX ix_catalogo_snap ON catalogo_snapshot (capturado_em, tipo, codigo);

CREATE TABLE termo (
    id             bigserial PRIMARY KEY,
    termo          text NOT NULL,
    termo_norm     text NOT NULL,
    categoria      text,
    origem         text,
    ativo          boolean NOT NULL DEFAULT true,
    excluido_por   text,
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
"""

# ── 5. Documentos e itens (§4) ──────────────────────────────────────────────────────
DESCOBERTA = """
CREATE TABLE documento (
    numero_controle_pncp text NOT NULL PRIMARY KEY,
    tipo_doc             tipo_documento NOT NULL,
    orgao                text,
    orgao_cnpj           text,
    uf                   text,
    ano                  int,
    data                 date,
    data_assinatura      date,
    data_fim_vigencia    date,
    data_atualizacao_pncp timestamptz,
    url_pncp             text,
    n_paginas            int,
    hash_arquivo         text,
    estado               estado_documento NOT NULL DEFAULT 'descoberto',
    n_itens              int NOT NULL DEFAULT 0,
    n_itens_sobreviventes int NOT NULL DEFAULT 0,
    descoberto_no_run_id bigint REFERENCES run(id),
    criado_em            timestamptz NOT NULL DEFAULT now(),
    atualizado_em        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_documento_estado ON documento (estado);
CREATE INDEX ix_documento_atualizacao ON documento (data_atualizacao_pncp DESC);

CREATE TABLE documento_termo (
    numero_controle_pncp text   NOT NULL REFERENCES documento(numero_controle_pncp) ON DELETE CASCADE,
    termo_id             bigint NOT NULL REFERENCES termo(id),
    PRIMARY KEY (numero_controle_pncp, termo_id)
);

CREATE TABLE item (
    item_key             text NOT NULL PRIMARY KEY,
    numero_controle_pncp text NOT NULL REFERENCES documento(numero_controle_pncp) ON DELETE CASCADE,
    numero_item          int  NOT NULL,
    descricao_api        text NOT NULL,
    unidade              text,
    quantidade           numeric(18,4),
    preco_unitario       numeric(18,4),
    preco_estimado       numeric(18,4),
    fornecedor           text,
    data_resultado       date,
    texto_hash           text NOT NULL,
    sobrevivente         boolean NOT NULL DEFAULT false,
    criado_em            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (numero_controle_pncp, numero_item)
);
CREATE INDEX ix_item_doc         ON item (numero_controle_pncp);
CREATE INDEX ix_item_texto_hash  ON item (texto_hash);
CREATE INDEX ix_item_sobrevivente ON item (sobrevivente) WHERE sobrevivente;

CREATE TABLE coleta_watermark (
    termo_id   bigint         NOT NULL REFERENCES termo(id) ON DELETE CASCADE,
    tipo_doc   tipo_documento NOT NULL,
    watermark  timestamptz    NOT NULL,
    atualizado_em timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (termo_id, tipo_doc)
);
"""

# ── 6. Classificação (§5) ───────────────────────────────────────────────────────────
CLASSIFICACAO = """
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

CREATE TABLE item_categoria (
    item_key  text NOT NULL REFERENCES item(item_key) ON DELETE CASCADE,
    categoria text NOT NULL,
    PRIMARY KEY (item_key, categoria)
);
CREATE INDEX ix_item_categoria_cat ON item_categoria (categoria);
"""

# ── 7. Extração (§6) ────────────────────────────────────────────────────────────────
EXTRACAO = """
CREATE TABLE documento_extracao (
    id            bigserial PRIMARY KEY,
    numero_controle_pncp text NOT NULL REFERENCES documento(numero_controle_pncp) ON DELETE CASCADE,
    estrategia    estrategia_extracao NOT NULL,
    itens_json    jsonb,
    n_paginas     int,
    n_paginas_ocr int,
    tokens_in     bigint NOT NULL DEFAULT 0,
    tokens_out    bigint NOT NULL DEFAULT 0,
    custo_usd     numeric(12,6) NOT NULL DEFAULT 0,
    duracao_ms    int,
    modelo        text,
    provedor      text,
    run_id        bigint REFERENCES run(id),
    criado_em     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (numero_controle_pncp, estrategia)
);

CREATE TABLE documento_pagina (
    numero_controle_pncp text NOT NULL REFERENCES documento(numero_controle_pncp) ON DELETE CASCADE,
    arquivo   text NOT NULL,
    pagina    int  NOT NULL,
    fonte     text NOT NULL,
    texto     text NOT NULL,
    n_chars   int  GENERATED ALWAYS AS (length(texto)) STORED,
    PRIMARY KEY (numero_controle_pncp, arquivo, pagina)
);
-- Explicitar o STORAGE não muda o padrão de `text`; documenta que o TOAST é o que segura os
-- 2,6 GB desta coluna, e protege contra alguém "otimizar" para MAIN um dia (02_SCHEMA §1).
ALTER TABLE documento_pagina ALTER COLUMN texto SET STORAGE EXTENDED;

CREATE TABLE item_enriquecido (
    item_key          text NOT NULL PRIMARY KEY REFERENCES item(item_key) ON DELETE CASCADE,
    descricao_final   text NOT NULL,
    fonte_descricao   text NOT NULL,
    preco_api         numeric(18,4),
    preco_pdf         numeric(18,4),
    divergencia_preco numeric(10,4),
    fornecedor        text,
    quantidade_pdf    numeric(18,4),
    status            status_enriquecimento NOT NULL,
    destino           destino_item NOT NULL,
    estrategia        estrategia_extracao NOT NULL,
    doc_status        estado_documento NOT NULL,
    run_id            bigint REFERENCES run(id),
    criado_em         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_enriq_destino    ON item_enriquecido (destino);
CREATE INDEX ix_enriq_estrategia ON item_enriquecido (estrategia);
"""

# ── 8. Pareamento (§7) ──────────────────────────────────────────────────────────────
PAREAMENTO = """
CREATE TABLE par (
    par_key       text NOT NULL PRIMARY KEY,
    tipo          tipo_catalogo NOT NULL,
    codigo        text NOT NULL,
    item_key      text NOT NULL REFERENCES item(item_key) ON DELETE CASCADE,
    categoria     text NOT NULL,
    score_bm25    real,
    score_cosseno real,
    sobreviveu    boolean NOT NULL DEFAULT false,
    score_rerank  real,
    decisao       decisao_rerank,
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

CREATE TABLE rotulo (
    id             bigserial PRIMARY KEY,
    par_key        text NOT NULL,
    texto_catalogo text NOT NULL,
    texto_item     text NOT NULL,
    score_rerank   real,
    decisao_final  text NOT NULL,
    origem         text NOT NULL,
    modelo         text,
    run_id         bigint REFERENCES run(id),
    criado_em      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (par_key, origem)
);

CREATE TABLE embedding_cache (
    texto_hash text NOT NULL,
    provedor   text NOT NULL,
    modelo     text NOT NULL,
    dimensao   int  NOT NULL,
    vetor      bytea NOT NULL,
    criado_em  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (texto_hash, provedor, modelo, dimensao)
);
"""

# ── 9. Resultado (§8) ───────────────────────────────────────────────────────────────
RESULTADO = """
CREATE TABLE grupo_item (
    id            bigserial PRIMARY KEY,
    tipo          tipo_catalogo NOT NULL,
    codigo        text NOT NULL,
    item_key      text NOT NULL REFERENCES item(item_key) ON DELETE CASCADE,
    par_key       text NOT NULL,
    posicao       int  NOT NULL,
    preco_unitario numeric(18,4),
    flag_preco    boolean NOT NULL DEFAULT false,
    motivo_flag   text,
    run_id        bigint NOT NULL REFERENCES run(id),
    criado_em     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, tipo, codigo, item_key)
);
CREATE INDEX ix_grupo_codigo ON grupo_item (tipo, codigo, posicao);
CREATE INDEX ix_grupo_run    ON grupo_item (run_id);

CREATE TABLE faixa_preco (
    categoria  text PRIMARY KEY,
    preco_min  numeric(18,4),
    preco_max  numeric(18,4),
    config_versao_id bigint REFERENCES config_versao(id)
);

CREATE TABLE export (
    id          bigserial PRIMARY KEY,
    run_id      bigint NOT NULL REFERENCES run(id),
    tipo        text NOT NULL,
    arquivo     text NOT NULL,
    n_linhas    int  NOT NULL,
    n_codigos   int  NOT NULL,
    hash_arquivo text,
    criado_em   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE export_snapshot (
    tipo                 tipo_catalogo NOT NULL,
    codigo               text NOT NULL,
    numero_controle_pncp text NOT NULL,
    numero_item          int  NOT NULL,
    export_id            bigint REFERENCES export(id),
    PRIMARY KEY (tipo, codigo, numero_controle_pncp, numero_item)
);
"""

BLOCOS = (ENUMS, CONFIG, EXECUCAO, CATALOGO, DESCOBERTA,
          CLASSIFICACAO, EXTRACAO, PAREAMENTO, RESULTADO)

TABELAS = (
    "export_snapshot", "export", "faixa_preco", "grupo_item",
    "embedding_cache", "rotulo", "par",
    "item_enriquecido", "documento_pagina", "documento_extracao",
    "item_categoria", "texto_classificacao",
    "coleta_watermark", "item", "documento_termo", "documento",
    "termo_codigo", "termo", "catalogo_snapshot", "catalogo_item",
    "llm_chamada", "erro_item", "run_log", "execucao_lock", "run_etapa", "run",
    "provedor_status", "capacidade_provedor", "provedor",
    "prompt_versao", "prompt", "config_valor", "config_versao",
)

TIPOS = (
    "capacidade", "acao_execucao", "status_etapa", "status_run", "modo_run",
    "decisao_final_par", "veredito_par", "decisao_rerank", "destino_item",
    "status_enriquecimento", "estrategia_extracao", "estado_documento",
    "tipo_documento", "tipo_catalogo",
)


def upgrade() -> None:
    for bloco in BLOCOS:
        op.execute(bloco)


def downgrade() -> None:
    # Ordem inversa da criação. `CASCADE` não é usado de propósito: se sobrou algo apontando
    # para uma destas tabelas, é melhor a migration falhar do que apagar em silêncio o que
    # alguém acrescentou depois.
    for tabela in TABELAS:
        op.execute(f"DROP TABLE IF EXISTS {tabela}")
    for tipo in TIPOS:
        op.execute(f"DROP TYPE IF EXISTS {tipo}")
