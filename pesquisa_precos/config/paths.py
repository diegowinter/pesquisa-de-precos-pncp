"""
Caminhos dos CSVs herdados — **exclusivo do importador** (`migracao/`) e de `tools/`.

Até a Fase 13 este módulo era a fonte única dos caminhos de TODA a pipeline: cada etapa lia e
escrevia em `data/`. Com o caminho `--fonte csv` removido (ADR-020), nenhum módulo de
`pesquisa_precos/steps/` importa isto, e nenhum deve voltar a importar — o banco é o único
meio de persistência. `tests/test_estrutura.py` guarda exatamente essa regra.

O que sobrou aqui é o mapa do acervo que ainda NÃO foi migrado: 1,6 milhão de itens que vivem
só nos CSVs, e que `migracao/` (21 passos) lê para popular o Postgres. Quando a migração
estiver feita e validada, este módulo sai junto com ela.

A raiz é derivada da posição DESTE arquivo (`<raiz>/pesquisa_precos/config/paths.py`), então
ela não muda quando um módulo é movido. `PESQUISA_PRECOS_DATA` permite apontar a pasta de
dados para outro lugar (útil em teste); sem ela, vale `<raiz>/data`.

Convenção dos nomes: o prefixo é a etapa que PRODUZIU o arquivo (`E2_ITENS` saiu da etapa 2) e
`CK_*` são checkpoints (estado de resumo, não saída).
"""

import os
from pathlib import Path

# <raiz>/pesquisa_precos/config/paths.py → parents[2] == <raiz>
RAIZ = Path(__file__).resolve().parents[2]

DATA = Path(os.getenv("PESQUISA_PRECOS_DATA") or (RAIZ / "data"))

CHECKPOINTS = DATA / "checkpoints"

# ── Etapa 0a — catálogo CATMAT/CATSER ────────────────────────────────────────────
E0A_CATALOGO = DATA / "0a_catalogo_filtrado.csv"
E0A_DELTA = DATA / "0a_catalogo_delta.csv"

# ── Etapa 1 — termos de busca ────────────────────────────────────────────────────
E1_TERMOS = DATA / "1_conceitos_termos.csv"
E1_CATEGORIA_POR_CODIGO = DATA / "1_categoria_por_codigo.csv"

# ── Etapa 2 — coleta no PNCP ─────────────────────────────────────────────────────
E2_ITENS = DATA / "2_itens_coletados.csv"
CK_2_CONCEITOS_EXTRA = CHECKPOINTS / "2_conceitos_extra.csv"
CK_2_WATERMARK = CHECKPOINTS / "2_watermark.csv"

# ── Etapa 3 — classificação ──────────────────────────────────────────────────────
E3_CLASSIFICADOS = DATA / "3_itens_classificados.csv"

# ── Etapa 4 — corte ──────────────────────────────────────────────────────────────
E4_SOBREVIVENTES = DATA / "4_itens_sobreviventes.csv"

# ── Etapa 5 — extração e enriquecimento ──────────────────────────────────────────
E5_PDF_TEXTO = DATA / "5_pdf_texto.csv"          # documento_pagina (texto por página)
E5_ENRIQUECIDOS = DATA / "5_itens_enriquecidos.csv"
E5_DESTINO = DATA / "5_itens_destino.csv"        # manter/revisar/descartar por item

# ── Etapa 6 — pares, reranker, validação LLM ─────────────────────────────────────
E6A_PARES = DATA / "6a_pares_candidatos.csv"
E6B_RERANKEADOS = DATA / "6b_pares_rerankeados.csv"
E6C_VALIDADOS = DATA / "6c_pares_validados.csv"
E6_ROTULOS = DATA / "6_rotulos_acumulados.csv"
CK_6A_EMB_CACHE = CHECKPOINTS / "6a_emb_cache.parquet"

# ── Etapa 7 — agrupamento ────────────────────────────────────────────────────────
E7_AGRUPADOS = DATA / "7_itens_agrupados.csv"
FAIXAS_PRECO = DATA / "config_faixas_preco.csv"  # curado à mão; migra para `faixa_preco`

# ── Etapa 8 — export PLASEG ──────────────────────────────────────────────────────
E8_SNAPSHOT = DATA / "8_export_snapshot.csv"     # baseline do --novos; migra para
                                                 # `export_snapshot` (m16)
