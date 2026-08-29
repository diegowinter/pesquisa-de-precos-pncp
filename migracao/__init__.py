"""
Migração one-shot dos CSVs para o PostgreSQL (Fase 2).

Cada passo é um script independente e resumível, na ordem das dependências de FK
(docs/05_MIGRACAO.md §2). Rodar um passo duas vezes não duplica nada.

    python -m migracao.m01_config_inicial
    python -m migracao.m02_prompts
    ...
    python -m migracao.validar

`python -m migracao` lista os passos e o que já foi feito.

**Nenhum CSV é apagado ou alterado.** A origem é somente-leitura durante toda a fase; o
roteiro de rollback (§7) depende disso.

Este pacote NÃO faz parte do pacote instalável (`pyproject.toml` inclui só `pesquisa_precos*`):
é código de uma vez só, rodado de dentro do repositório, e não deve ir junto num deploy.
"""

PASSOS = (
    ("m01_config_inicial", ".env → config_version, config_value, provider"),
    ("m02_prompts", "core/prompts.py → prompt, prompt_version (v1 active)"),
    ("m03_run_historico", "run sintético 'acervo migrado v2/v3'"),
    ("m04_catalogo", "0a_catalogo_filtrado + 1_categoria_por_codigo → catalogo_item"),
    ("m05_termos", "1_conceitos_termos → termo, termo_codigo"),
    ("m06_watermark", "checkpoints/2_watermark → collection_watermark"),
    ("m07_documentos_itens", "2_itens_coletados → documento, documento_termo, item"),
    ("m08_classificacao", "3_itens_classificados → texto_classificacao, item_categoria"),
    ("m09_sobreviventes", "4_itens_sobreviventes → item.sobrevivente"),
    ("m11_enriquecidos", "5_itens_enriquecidos + destino → item_enriquecido, documento_extracao"),
    ("m12_pares", "6a + 6b + 6c → par"),
    ("m13_rotulos", "6_rotulos_acumulados → label"),
    ("m14_embeddings", "checkpoints/6a_emb_cache.parquet → embedding_cache"),
    ("m15_grupos", "7_itens_agrupados → grupo_item"),
    ("m16_export_snapshot", "8_export_snapshot → export_snapshot"),
    ("m17_faixas", "config_faixas_preco → faixa_preco"),
)
