"""item pertence a compra, nao a ata (ADR-024)

A API do PNCP entrega itens por COMPRA e nao tem rota de itens por ata (confirmado na
especificacao OpenAPI). A etapa 2 pendurava a lista inteira da compra em cada ata que ela
gerou: o pregao 507 da Embrapa tem 88 itens e 25 atas, e as 25 receberam os mesmos 82 itens
homologados. Medido no acervo: 8,40x de duplicacao em ata, 1,00x em contrato, 4,11x no total.

O que muda:

  item.item_key         passa a ser  <chave_compra>::<numero_item>  (era <documento>::<item>)
  item.compra_key       coluna nova, SEM FK — compra nao e linha em `documento`
  item.numero_controle_pncp   SAI, junto com o FK para `documento`. Era essa amarra que
                        criava a duplicacao: cada linha ja nascia presa a uma ata.
  item_enriquecido      ganha `numero_controle_pncp` (a ata onde o item foi achado) e a PK
                        vira (item_key, numero_controle_pncp) — o vinculo ata<->item nasce
                        na etapa 5, que e onde ele e descoberto.
  documento.compra_key  coluna GERADA, para nenhum SQL repetir a derivacao.

Colapso esperado: `item` de 311.094 para ~75.711 linhas; `item_categoria` deduplicada por
item_key. `par` e `grupo_item` estao vazias.

DOWNGRADE NAO RESTAURA AS LINHAS COLAPSADAS. A duplicacao era copia da mesma informacao, mas
a associacao original item->ata nao e reconstruivel: era justamente a associacao ERRADA que
esta migration existe para remover. O downgrade recria as colunas e o formato antigo de chave
apontando cada item para UMA ata da compra (a de menor sequencial), o que basta para o schema
voltar a casar com o codigo anterior. Quem precisa do estado exato usa o backup.

Revision ID: 0013
Revises: 0012
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# Derivacao da chave de compra em SQL. ESPELHA `core.collection.urls.chave_compra` e existe
# so aqui, dentro da migration — o codigo da aplicacao usa a funcao Python. Um `regexp_replace`
# equivalente escrito a mao durante a investigacao perdeu o ano por um backslash comido pelo
# shell e produziu contagens erradas sem levantar erro; por isso aqui e `left`/`strpos`, sem
# backreference.
_CHAVE = "left({col}, strpos({col}, '/') + 4)"

UP = f"""
-- ── documento.compra_key: coluna gerada, uma derivacao so ────────────────────────────
ALTER TABLE documento
    ADD COLUMN compra_key text
    GENERATED ALWAYS AS ({_CHAVE.format(col="numero_controle_pncp")}) STORED;
CREATE INDEX ix_documento_compra_key ON documento (compra_key);

-- ── item: colapsa para (compra, numero_item) ─────────────────────────────────────────
ALTER TABLE item ADD COLUMN compra_key text;
UPDATE item SET compra_key = {_CHAVE.format(col="numero_controle_pncp")};

-- Guarda o vinculo antigo antes de descartar: a etapa 5 vai reconstrui-lo de verdade, mas
-- ate la `item_enriquecido` precisa apontar para ALGUMA ata, e a herdada e a melhor
-- aproximacao disponivel.
CREATE TEMP TABLE _de_para AS
SELECT item_key AS antigo,
       compra_key || '::' || numero_item::text AS novo,
       numero_controle_pncp
  FROM item;
CREATE INDEX ON _de_para (antigo);

-- item_categoria e item_enriquecido apontam para item_key: reescreve ANTES de colapsar.
ALTER TABLE item_categoria   DROP CONSTRAINT IF EXISTS item_categoria_item_key_fkey;
ALTER TABLE item_enriquecido DROP CONSTRAINT IF EXISTS item_enriquecido_item_key_fkey;
ALTER TABLE par              DROP CONSTRAINT IF EXISTS par_item_key_fkey;
ALTER TABLE grupo_item       DROP CONSTRAINT IF EXISTS grupo_item_item_key_fkey;

-- `item_enriquecido` passa a dizer em QUAL ata o item foi achado.
-- A PK antiga sai ANTES do UPDATE: duas atas da mesma compra colapsam na mesma `item_key`,
-- e com `PRIMARY KEY (item_key)` ainda de pe o proprio UPDATE viola a unicidade.
ALTER TABLE item_enriquecido DROP CONSTRAINT item_enriquecido_pkey;
ALTER TABLE item_enriquecido ADD COLUMN numero_controle_pncp text;
UPDATE item_enriquecido e SET numero_controle_pncp = d.numero_controle_pncp
  FROM _de_para d WHERE d.antigo = e.item_key;
UPDATE item_enriquecido e SET item_key = d.novo
  FROM _de_para d WHERE d.antigo = e.item_key;
DELETE FROM item_enriquecido WHERE numero_controle_pncp IS NULL;

ALTER TABLE item_enriquecido
    ALTER COLUMN numero_controle_pncp SET NOT NULL,
    ADD PRIMARY KEY (item_key, numero_controle_pncp),
    ADD FOREIGN KEY (numero_controle_pncp)
        REFERENCES documento(numero_controle_pncp) ON DELETE CASCADE;

-- item_categoria: reescreve a chave e deduplica (o mesmo item vinha por N atas).
CREATE TEMP TABLE _cat AS
SELECT DISTINCT d.novo AS item_key, ic.categoria
  FROM item_categoria ic JOIN _de_para d ON d.antigo = ic.item_key;
TRUNCATE item_categoria;
INSERT INTO item_categoria (item_key, categoria) SELECT item_key, categoria FROM _cat;

-- Agora o colapso de `item`. `DISTINCT ON` fica com a linha da ata de menor numero de
-- controle; preco/fornecedor/quantidade sao identicos entre as copias (vem da mesma chamada
-- de /resultados da compra), entao qual sobrevive nao altera o dado.
CREATE TEMP TABLE _item AS
SELECT DISTINCT ON (compra_key, numero_item)
       compra_key || '::' || numero_item::text AS item_key,
       compra_key, numero_item, descricao_api, unidade, quantidade, preco_unitario,
       preco_estimado, fornecedor, data_resultado, texto_hash, sobrevivente, created_at
  FROM item
 ORDER BY compra_key, numero_item, numero_controle_pncp;

TRUNCATE item;
ALTER TABLE item
    DROP CONSTRAINT IF EXISTS item_numero_controle_pncp_numero_item_key,
    DROP COLUMN numero_controle_pncp;
ALTER TABLE item
    ALTER COLUMN compra_key SET NOT NULL,
    ADD CONSTRAINT item_compra_key_numero_item_key UNIQUE (compra_key, numero_item);

INSERT INTO item (item_key, compra_key, numero_item, descricao_api, unidade, quantidade,
                  preco_unitario, preco_estimado, fornecedor, data_resultado, texto_hash,
                  sobrevivente, created_at)
SELECT item_key, compra_key, numero_item, descricao_api, unidade, quantidade, preco_unitario,
       preco_estimado, fornecedor, data_resultado, texto_hash, sobrevivente, created_at
  FROM _item;

CREATE INDEX ix_item_compra_key ON item (compra_key);

-- Recria os FKs que apontam para item_key.
ALTER TABLE item_categoria
    ADD FOREIGN KEY (item_key) REFERENCES item(item_key) ON DELETE CASCADE;
ALTER TABLE item_enriquecido
    ADD FOREIGN KEY (item_key) REFERENCES item(item_key) ON DELETE CASCADE;
ALTER TABLE par
    ADD FOREIGN KEY (item_key) REFERENCES item(item_key) ON DELETE CASCADE;
ALTER TABLE grupo_item
    ADD FOREIGN KEY (item_key) REFERENCES item(item_key) ON DELETE CASCADE;
"""

DOWN = """
ALTER TABLE item_categoria   DROP CONSTRAINT IF EXISTS item_categoria_item_key_fkey;
ALTER TABLE item_enriquecido DROP CONSTRAINT IF EXISTS item_enriquecido_item_key_fkey;
ALTER TABLE par              DROP CONSTRAINT IF EXISTS par_item_key_fkey;
ALTER TABLE grupo_item       DROP CONSTRAINT IF EXISTS grupo_item_item_key_fkey;

-- Uma ata por compra (a de menor numero de controle) para o item voltar a ter documento.
CREATE TEMP TABLE _uma_ata AS
SELECT DISTINCT ON (compra_key) compra_key, numero_controle_pncp
  FROM documento ORDER BY compra_key, numero_controle_pncp;

ALTER TABLE item ADD COLUMN numero_controle_pncp text;
UPDATE item i SET numero_controle_pncp = a.numero_controle_pncp
  FROM _uma_ata a WHERE a.compra_key = i.compra_key;
DELETE FROM item WHERE numero_controle_pncp IS NULL;

CREATE TEMP TABLE _volta AS
SELECT item_key AS antigo,
       numero_controle_pncp || '::' || numero_item::text AS novo
  FROM item;
CREATE INDEX ON _volta (antigo);

UPDATE item_categoria c SET item_key = v.novo FROM _volta v WHERE v.antigo = c.item_key;
DELETE FROM item_categoria c WHERE NOT EXISTS (
    SELECT 1 FROM _volta v WHERE v.novo = c.item_key);

ALTER TABLE item_enriquecido DROP CONSTRAINT item_enriquecido_pkey;
UPDATE item_enriquecido e SET item_key = v.novo FROM _volta v WHERE v.antigo = e.item_key;
DELETE FROM item_enriquecido e WHERE NOT EXISTS (
    SELECT 1 FROM _volta v WHERE v.novo = e.item_key);

-- O modelo antigo so cabe UM enriquecido por item. O novo permite o mesmo item achado em
-- atas diferentes, e no acervo isso existe — logo a volta PERDE linhas, e nao ha como nao
-- perder. Fica a da ata que o downgrade escolheu para o item (`_uma_ata`); no empate, a mais
-- recente. Esta e a perda que o cabecalho da migration avisa.
DELETE FROM item_enriquecido e
 WHERE ctid NOT IN (
     SELECT DISTINCT ON (x.item_key) x.ctid
       FROM item_enriquecido x
       LEFT JOIN item i ON i.item_key = x.item_key
      ORDER BY x.item_key,
               (x.numero_controle_pncp = i.numero_controle_pncp) DESC NULLS LAST,
               x.created_at DESC);

ALTER TABLE item_enriquecido
    DROP CONSTRAINT IF EXISTS item_enriquecido_numero_controle_pncp_fkey,
    DROP COLUMN numero_controle_pncp;
ALTER TABLE item_enriquecido ADD PRIMARY KEY (item_key);

UPDATE item SET item_key = numero_controle_pncp || '::' || numero_item::text;

ALTER TABLE item
    DROP CONSTRAINT IF EXISTS item_compra_key_numero_item_key,
    DROP COLUMN compra_key,
    ALTER COLUMN numero_controle_pncp SET NOT NULL,
    ADD CONSTRAINT item_numero_controle_pncp_numero_item_key
        UNIQUE (numero_controle_pncp, numero_item),
    ADD FOREIGN KEY (numero_controle_pncp)
        REFERENCES documento(numero_controle_pncp) ON DELETE CASCADE;

DROP INDEX IF EXISTS ix_item_compra_key;
DROP INDEX IF EXISTS ix_documento_compra_key;
ALTER TABLE documento DROP COLUMN compra_key;

ALTER TABLE item_categoria
    ADD FOREIGN KEY (item_key) REFERENCES item(item_key) ON DELETE CASCADE;
ALTER TABLE item_enriquecido
    ADD FOREIGN KEY (item_key) REFERENCES item(item_key) ON DELETE CASCADE;
ALTER TABLE par
    ADD FOREIGN KEY (item_key) REFERENCES item(item_key) ON DELETE CASCADE;
ALTER TABLE grupo_item
    ADD FOREIGN KEY (item_key) REFERENCES item(item_key) ON DELETE CASCADE;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
