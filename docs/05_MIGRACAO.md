# 05 — Migração CSV → PostgreSQL

O acervo atual é o ativo mais valioso do projeto: 1,6M itens coletados, 302k enriquecidos com
LLM, 250k rótulos de pareamento, 888k páginas de texto extraído. Migrar sem perder nada é o
objetivo desta fase — **e nenhum CSV é apagado durante o processo**.

## 1. Regras invioláveis

1. **Idempotente.** Rodar duas vezes não duplica. Todo `INSERT` usa `ON CONFLICT DO NOTHING` ou
   `DO UPDATE` explícito.
2. **Resumível.** Grava o offset processado; interromper e retomar não recomeça do zero.
3. **Somente leitura na origem.** Os CSVs em `data/` não são modificados nem removidos.
4. **`pg_dump` antes de cada agregado.** Barato, e o custo de não ter é altíssimo.
5. **Validação obrigatória por agregado** — contagem, amostragem e checagem de integridade
   referencial antes de seguir para o próximo.
6. **Streaming, nunca `pd.read_csv` inteiro.** `2_itens_coletados.csv` tem 746 MB e
   `5_pdf_texto.csv` tem 2,6 GB. Usar `csv.DictReader` + `COPY` em lotes.

> `csv.field_size_limit(10**9)` é necessário — há campos de texto de PDF gigantes.

## 2. Ordem de migração

Segue as dependências de FK. Cada passo é um script independente em `migracao/`.

| # | Script | Origem | Destino | Linhas |
|---|---|---|---|---:|
| 1 | `m01_config_inicial.py` | `.env` | `config_versao`, `config_valor`, `provedor` | — |
| 2 | `m02_prompts.py` | `core/prompts.py` | `prompt`, `prompt_versao` (v1 ativa) | ~12 |
| 3 | `m03_run_historico.py` | — | `run` sintético "acervo migrado v2/v3" | 1 |
| 4 | `m04_catalogo.py` | `0a_catalogo_filtrado.csv` + `1_categoria_por_codigo.csv` | `catalogo_item` | 2.212 |
| 5 | `m05_termos.py` | `1_conceitos_termos.csv` | `termo`, `termo_codigo` | 499 / ~3.000 |
| 6 | `m06_watermark.py` | `checkpoints/2_watermark.csv` | `coleta_watermark` | ~1.000 |
| 7 | `m07_documentos_itens.py` | `2_itens_coletados.csv` + `checkpoints/2_conceitos_extra.csv` | `documento`, `documento_termo`, `item` | 68.163 / 1.613.517 |
| 8 | `m08_classificacao.py` | `3_itens_classificados.csv` | `texto_classificacao`, `item_categoria` | ~320k |
| 9 | `m09_sobreviventes.py` | `4_itens_sobreviventes.csv` | `item.sobrevivente`, `documento.n_itens_sobreviventes` | 302.514 |
| 10 | `m10_texto_pdf.py` | `5_pdf_texto.csv` | `documento_pagina` | 888.656 |
| 11 | `m11_enriquecidos.py` | `5_itens_enriquecidos.csv` + `5_itens_destino.csv` | `item_enriquecido`, `documento_extracao` | 302.514 |
| 12 | `m12_pares.py` | `6a`+`6b`+`6c` | `par` | ~250.000 |
| 13 | `m13_rotulos.py` | `6_rotulos_acumulados.csv` | `rotulo` | 250.085 |
| 14 | `m14_embeddings.py` | `checkpoints/6a_emb_cache.parquet` | `embedding_cache` | ~305.000 |
| 15 | `m15_grupos.py` | `7_itens_agrupados.csv` | `grupo_item` | 118.722 |
| 16 | `m16_export_snapshot.py` | `8_export_snapshot.csv` | `export_snapshot` | 118.722 |
| 17 | `m17_faixas.py` | `config_faixas_preco.csv` | `faixa_preco` | ~10 |

## 3. Detalhes por passo crítico

### m04 — Catálogo

`0a_catalogo_filtrado.csv` tem BOM (`utf-8-sig`). Colunas:
`tipo, codigo, codigo_pdm, nome_pdm, descricao, codigo_grupo, nome_grupo, nome_classe`.

A `categoria` vem de `1_categoria_por_codigo.csv` (`tipo, codigo, categoria`) — é a fonte
canônica por item usada pela etapa 6a. Fazer o join na migração.

`0a_catalogo_delta.csv` indica códigos `removido` → `catalogo_item.ativo = false`.

### m05 — Termos

`1_conceitos_termos.csv` tem `conceito, categoria, termos, codigos_catalogo, origem`.
**`conceito` é hoje idêntico a `termos`** — o conceito como entidade separada não existe mais.
Usar `termos` como `termo`; ignorar `conceito`.

`codigos_catalogo` é uma lista serializada → explodir em `termo_codigo`. Verificar o separador
real no arquivo antes de assumir (`;` ou `,`).

`termo_norm` = minúsculo, sem acento (NFKD + remoção de combining), espaços normalizados.

### m07 — Documentos e itens (o passo mais pesado)

Origem: `2_itens_coletados.csv`, 746 MB, 1.613.517 linhas, achatado (documento repetido por item).

```
item_key, tipo_doc, numeroControlePNCP, numeroItem, descricao_api, unidade,
quantidade, preco_unitario, orgao, uf, data, conceitos_origem, pasta_arquivos,
ano, orgao_cnpj, data_fim_vigencia, data_assinatura, preco_estimado, fornecedor, data_resultado
```

Duas passadas em streaming:

**Passada 1 — documentos.** Agrupa por `numeroControlePNCP`, pega os campos de documento da
primeira ocorrência, conta itens. `COPY` em lotes de 5.000.

**Passada 2 — itens.** Uma linha por item. Calcular **`texto_hash` aqui**:

```python
texto_hash = sha1(f"{norm(descricao_api)}|{norm(unidade)}".encode()).hexdigest()
```

`norm` = minúsculo, sem acento, espaços colapsados. **Esta função precisa ser exatamente a mesma
usada pela etapa 3 em produção** — senão o dedup permanente não funciona e a primeira execução
reclassifica 320k textos já pagos. Extrair para `core/textos.py` e usar dos dois lados.

**`conceitos_origem`** → `documento_termo`. Consolidar também
`checkpoints/2_conceitos_extra.csv` (os conceitos acrescentados por dedup de documento).
Hoje isso é feito por `coleta_pncp.carregar_itens_coletados()` — replicar essa lógica.

**`pasta_arquivos`** (caminho absoluto do PDF) **não é migrado como caminho.** Registrar em
`documento.url_pncp` a URL reconstruída do PNCP. Ver [§5](#5-o-que-fazer-com-os-pdfs-antigos).

**`data_atualizacao_pncp`** não existe no CSV v2 — deixar `NULL`. O watermark vem do m06.

### m08 — Classificação

`3_itens_classificados.csv` é **por `item_key`** (1,6M linhas), mas a tabela destino é **por
texto** (~320k). Colapsar:

1. Ler `item_key → (categorias, confianca)`.
2. Fazer o join com `item.texto_hash` (já no banco após m07).
3. Agrupar por `texto_hash`; em caso de divergência entre itens do mesmo texto, vencer a
   classificação mais frequente (deveria ser unânime — divergência indica que o dedup atual não
   estava perfeito; **logar quantos casos**, é informação útil).
4. `categorias` é string separada → converter para `text[]`.
5. `modelo` e `provedor`: preencher com o que foi usado na v2/v3 (constante), `prompt_versao_id`
   apontando para a v1 migrada.

Depois recomputar `item_categoria` por SQL puro (ver [02_SCHEMA.md §5](02_SCHEMA.md#5-classificação)).

### m10 — Texto de PDF (2,6 GB)

`5_pdf_texto.csv`: `doc_key, arquivo, pagina, fonte, texto`. 888.656 linhas.

- `doc_key` é o **caminho absoluto** da pasta → mapear para `numero_controle_pncp`.
  O mapa vem de `2_itens_coletados.csv` (`pasta_arquivos` → `numeroControlePNCP`).
  **Documentos cujo `doc_key` não mapear devem ser contados e reportados**, não silenciados.
- `COPY` em lotes de 1.000 (linhas grandes).
- Considerar `ALTER TABLE documento_pagina ALTER COLUMN texto SET STORAGE EXTENDED` (padrão
  para `text`, mas explicitar não custa).
- Rodar `VACUUM ANALYZE` ao final.

Estimativa pós-migração: ~700 MB–1 GB com compressão TOAST.

### m11 — Itens enriquecidos

Juntar `5_itens_enriquecidos.csv` (`item_key, descricao_final, fonte_descricao, preco_api,
preco_pdf, divergencia_preco, paginas_ocr, enriquecimento`) com `5_itens_destino.csv`
(`item_key, enriquecimento, doc_status, destino`).

- `enriquecimento` → `status` (mapeia 1:1 para o enum `status_enriquecimento`).
- `estrategia` = `'janela'` para todo o acervo migrado (foi o único caminho usado).
- `paginas_ocr` → `documento_extracao.n_paginas_ocr`, agregado por documento.
- `documento_extracao`: uma linha por documento com `estrategia='janela'`,
  custo/tokens `NULL` (não foram medidos na v2/v3 — não inventar valores).
- `documento.estado`: derivar de `doc_status` (`ok`→`extraido`, `suspeito`→`suspeito`,
  `ilegivel`→`ilegivel`).

### m12 — Pares

Três arquivos com o mesmo `par_key`:

| Arquivo | Colunas | Linhas |
|---|---|---:|
| `6a_pares_candidatos.csv` | `par_key, codigo, item_key, categoria, score_bm25, score_cosseno, sobreviveu` | 220.781 |
| `6b_pares_rerankeados.csv` | `par_key, score_rerank, decisao` | 250.114 |
| `6c_pares_validados.csv` | `par_key, mesmo_item, justificativa` | 57.545 |

**Atenção:** a 6b tem *mais* linhas que a 6a (250k vs 220k) porque acumula entre execuções
resumíveis. Migrar com `6b` como base do conjunto de `par_key` e fazer `LEFT JOIN` com 6a e 6c.
Par sem correspondente em 6a fica com scores `NULL` — registrar quantos.

`codigo` no CSV não traz o `tipo` — resolver pelo join com `catalogo_item` (código é único no
catálogo filtrado; **validar essa premissa** e abortar se houver colisão).

`decisao_final`: `confirmado` quando `decisao='aceito'` OU `mesmo_item='sim'`.

### m14 — Cache de embeddings

`checkpoints/6a_emb_cache.parquet`, chaveado por `sha1(texto)`.

**A chave nova inclui provedor, modelo e dimensão.** Ao migrar, preencher com os valores que
foram efetivamente usados: `provedor='gpu_caseira'`, `modelo='BAAI/bge-m3'`, `dimensao=1024`
(confirmar no parquet antes). Errar isso invalida silenciosamente 305k embeddings pagos em GPU.

Vetor → `float16` little-endian em `bytea`.

## 4. Validação por agregado

Cada script termina com um relatório. Nada de "migrou, deve estar ok".

```python
def validar(conn):
    checks = {
        "documento":         (68_163,   "SELECT count(*) FROM documento"),
        "item":              (1_613_517,"SELECT count(*) FROM item"),
        "item_sobrevivente": (302_514,  "SELECT count(*) FROM item WHERE sobrevivente"),
        "item_enriquecido":  (302_514,  "SELECT count(*) FROM item_enriquecido"),
        "grupo_item":        (118_722,  "SELECT count(*) FROM grupo_item"),
        "rotulo":            (250_085,  "SELECT count(*) FROM rotulo"),
        "pagina":            (888_656,  "SELECT count(*) FROM documento_pagina"),
    }
    # integridade referencial
    orfaos = {
        "item sem documento":      "SELECT count(*) FROM item i LEFT JOIN documento d USING (numero_controle_pncp) WHERE d IS NULL",
        "enriquecido sem item":    "SELECT count(*) FROM item_enriquecido e LEFT JOIN item i USING (item_key) WHERE i IS NULL",
        "par sem item":            "SELECT count(*) FROM par p LEFT JOIN item i USING (item_key) WHERE i IS NULL",
        "grupo sem par":           "SELECT count(*) FROM grupo_item g LEFT JOIN par p USING (par_key) WHERE p IS NULL",
        "item sem texto_hash":     "SELECT count(*) FROM item WHERE texto_hash IS NULL OR texto_hash = ''",
    }
```

**Toda contagem de órfãos deve ser zero.** Divergência na contagem total é aceitável se
explicada (ex.: linhas duplicadas no CSV) — mas precisa ser *explicada*, não ignorada.

## 5. O que fazer com os PDFs antigos

Situação atual: ~90% das linhas apontam para caminhos **absolutos** em
`C:\Users\diego\Documents\dev\plaseg\itens-via-script\itens-contratos-atas-v2\data\arquivos\`
— 111 GB que **não** foram movidos para este repositório.

**Decisão: não migrar os PDFs. Migrar o texto já extraído (m10) e reconstruir a URL do PNCP.**

Justificativa:
- o texto extraído já está em `5_pdf_texto.csv` e cobre ~95% dos reprocessamentos realistas;
- o novo desenho descarta o PDF logo após a extração de qualquer forma;
- mover 111 GB e reescrever caminhos em CSVs de centenas de MB é caro e arriscado;
- o que falta pode ser rebaixado do PNCP sob demanda a partir de `numero_controle_pncp`.

**Ação necessária:** implementar `core/coleta/urls.py::url_documento(numero_controle_pncp, tipo_doc)`
e preencher `documento.url_pncp` na migração. Sem isso o rebaixamento sob demanda não existe e a
decisão acima fica sem rede de segurança.

Enquanto a pasta v2 não for movida nem apagada, os caminhos antigos continuam válidos — mas o
sistema novo **não deve depender deles**.

## 6. Corte de virada

Depois que todos os agregados validarem:

1. `pg_dump` completo, guardado fora do repositório.
2. Rodar as etapas 7 e 8 **do banco** e comparar o XLSX com `8_itens_plaseg.xlsx`.
   Diferenças precisam ser explicáveis linha a linha.
3. Semear `export_snapshot` a partir do último export **oficial** (m16), para que o primeiro
   `--novos` do sistema novo não marque 118k linhas como novidade.
4. Mover `data/*.csv` para `data/_migrado/` (não apagar) e marcar como somente leitura.
5. Só então as etapas passam a escrever exclusivamente no banco.

## 7. Roteiro de rollback

Se a fase 2 der errado, o caminho de volta é:

1. Os CSVs originais estão intactos em `data/` (ou `data/_migrado/`).
2. O código da Fase 1 (CLI + CSV) está na tag `fase-1-final`.
3. `git checkout fase-1-final` restaura o pipeline funcional.
4. Nenhum dado é perdido, porque nada foi apagado.

Manter essa tag até a Fase 3 estar validada em uso real.
