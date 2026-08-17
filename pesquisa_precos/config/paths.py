"""
Caminhos do projeto — fonte única da verdade.

Antes da Fase 0 cada script numerado tinha o seu próprio
`DATA = Path(__file__).resolve().parent / "data"`. Isso amarrava a localização dos dados à
profundidade do arquivo na árvore: mover um script uma pasta para dentro fazia a pipeline
apontar para uma pasta `data/` inexistente — e o sintoma seria "etapa não encontrou a
entrada", não "caminho errado". Por isso este módulo vem ANTES de qualquer movimentação de
arquivo: com os caminhos centralizados, mover módulo deixa de ser caça a bug.

A raiz é derivada da posição DESTE arquivo (`<raiz>/pesquisa_precos/config/paths.py`), então
ela não muda quando um módulo de etapa é movido. `PESQUISA_PRECOS_DATA` permite apontar a
pasta de dados para outro lugar (útil em teste); sem ela, vale `<raiz>/data`.

Convenção dos nomes: o prefixo é a etapa que PRODUZ o arquivo (`E2_ITENS` sai da etapa 2),
`CK_*` são checkpoints (estado de resumo, não saída) e `ERROS_*` são registros de falha por
item. Nada aqui cria pasta: quem grava é que chama `garantir_pastas()` ou o `EscritorSeguro`.
"""

import os
from pathlib import Path

# <raiz>/pesquisa_precos/config/paths.py → parents[2] == <raiz>
RAIZ = Path(__file__).resolve().parents[2]

DATA = Path(os.getenv("PESQUISA_PRECOS_DATA") or (RAIZ / "data"))

CHECKPOINTS = DATA / "checkpoints"
ERROS = DATA / "erros"
# PDFs baixados pela etapa 2. Só os documentos coletados a partir da v3 vivem aqui; ~90% do
# acervo herdado aponta para caminhos ABSOLUTOS na pasta do repositório v2, que não foi movida
# (ver CLAUDE.md). Por isso `pasta_arquivos` nos CSVs continua sendo lida como veio, nunca
# reconstruída a partir desta constante.
ARQUIVOS = DATA / "arquivos"

# ── Etapa 0a — catálogo CATMAT/CATSER ────────────────────────────────────────────
E0A_PARQUET_MATERIAIS = DATA / "0a_catalogo_materiais.parquet"
E0A_PARQUET_SERVICOS = DATA / "0a_catalogo_servicos.parquet"
E0A_META = DATA / "0a_catalogo_meta.json"
E0A_CATALOGO = DATA / "0a_catalogo_filtrado.csv"
E0A_SNAPSHOT = DATA / "0a_catalogo_snapshot.csv"
E0A_DELTA = DATA / "0a_catalogo_delta.csv"
# Partes de download por página (resumibilidade da 0a): <CK_0A_PARTS>_<tipo>/
CK_0A_PARTS = CHECKPOINTS  # subpasta "0a_parts_<tipo>" é montada pela etapa

# ── Etapa 1 — termos de busca ────────────────────────────────────────────────────
E1_TERMOS = DATA / "1_conceitos_termos.csv"
E1_CATEGORIA_POR_CODIGO = DATA / "1_categoria_por_codigo.csv"
CK_1_TERMOS_ITEM = CHECKPOINTS / "1_termos_item.csv"
ERROS_1 = ERROS / "1_erros.csv"

# ── Etapa 2 — coleta no PNCP ─────────────────────────────────────────────────────
E2_ITENS = DATA / "2_itens_coletados.csv"
CK_2_PROGRESSO = CHECKPOINTS / "2_progresso.csv"
CK_2_CONCEITOS_EXTRA = CHECKPOINTS / "2_conceitos_extra.csv"
CK_2_WATERMARK = CHECKPOINTS / "2_watermark.csv"
CK_2_PENDENTES = CHECKPOINTS / "2_pendentes.csv"
ERROS_2 = ERROS / "2_erros.csv"

# ── Etapa 3 — classificação ──────────────────────────────────────────────────────
E3_CLASSIFICADOS = DATA / "3_itens_classificados.csv"
ERROS_3 = ERROS / "3_erros.csv"

# ── Etapa 4 — corte ──────────────────────────────────────────────────────────────
E4_SOBREVIVENTES = DATA / "4_itens_sobreviventes.csv"
E4_RELATORIO = DATA / "4_relatorio_corte.csv"

# ── Etapa 5 — extração e enriquecimento (Fase 8, ADR-010/011/012: estratégias plugáveis:
# janela|completa|visao, roteadas por documento; download+OCR+extração+descarte do PDF numa
# etapa só) ────────────────────────────────────────────────────────────────────────────────
E5_PDF_TEXTO = DATA / "5_pdf_texto.csv"          # documento_pagina (texto por página)
E5_ENRIQUECIDOS = DATA / "5_itens_enriquecidos.csv"  # contrato de saída (ADR-010) — item_key
E5_DESTINO = DATA / "5_itens_destino.csv"        # manter/revisar/descartar por item
E5_DOC_EXTRACAO = DATA / "5_documento_extracao.csv"  # 1 linha por (documento, estratégia)
ERROS_5 = ERROS / "5_erros.csv"

# ── Etapa 6 — pares, reranker, validação LLM ─────────────────────────────────────
E6A_PARES = DATA / "6a_pares_candidatos.csv"
E6B_RERANKEADOS = DATA / "6b_pares_rerankeados.csv"
E6C_VALIDADOS = DATA / "6c_pares_validados.csv"
E6_ROTULOS = DATA / "6_rotulos_acumulados.csv"
CK_6A_EMB_CACHE = CHECKPOINTS / "6a_emb_cache.parquet"
ERROS_6C = ERROS / "6c_erros.csv"

# ── Etapa 7 — agrupamento ────────────────────────────────────────────────────────
E7_AGRUPADOS = DATA / "7_itens_agrupados.csv"
E7_RELATORIO = DATA / "7_relatorio_grupos.csv"
FAIXAS_PRECO = DATA / "config_faixas_preco.csv"  # opcional, curado à mão

# ── Etapa 8 — export PLASEG ──────────────────────────────────────────────────────
E8_XLSX = DATA / "8_itens_plaseg.xlsx"
E8_CSV = DATA / "8_itens_plaseg.csv"
E8_NOVOS_XLSX = DATA / "8_itens_plaseg_novos.xlsx"
E8_NOVOS_CSV = DATA / "8_itens_plaseg_novos.csv"
E8_SNAPSHOT = DATA / "8_export_snapshot.csv"


def garantir_pastas() -> None:
    """Cria as pastas de dados/checkpoints/erros (idempotente)."""
    for pasta in (DATA, CHECKPOINTS, ERROS):
        pasta.mkdir(parents=True, exist_ok=True)
