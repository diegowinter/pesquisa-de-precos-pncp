"""
Motor de pareamento: catálogo × itens → pares sobreviventes (BM25 + cosseno + corte).

Implementação COMPARTILHADA da capacidade `pareamento` (Fase 11, ADR-019). Roda nos dois
lados, sem bifurcação: dentro do container via `PareamentoEmProcessoAdapter`, ou dentro do
`servidor_pareamento.py`, na máquina que tem a GPU.

⚠ A REGRA QUE NÃO PODE SER PERDIDA NA MUDANÇA DE LADO
O corte top-K + piso é aplicado EM STREAMING, por código, direto nas matrizes numpy —
NUNCA depois de materializar o produto cartesiano num DataFrame. Um `aplicar_corte` pós-hoc
com `groupby().rank()` já causou um `MemoryError` real com ~33M linhas, e o fóssil daquele bug
(`6a_pares_candidatos_PRECORTE.csv`, 3,5 GB) ainda está no `data/`. Externalizar o processamento
MOVE essa restrição de lado; não a remove — a memória do servidor é tão finita quanto a do
container.

Regras de negócio preservadas da etapa 6a:
  - o produto é RESTRITO à mesma categoria (item multi-label pareia em todas as suas);
  - NÃO há dedup de pares (é regra de negócio, não descuido);
  - o par sobrevive se `max(bm25_norm, cosseno) >= piso` — basta um sinal dizer "pode ser";
  - o BM25 é normalizado min-max DENTRO da categoria, não global.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from pesquisa_precos.core.pareamento.indice_lexical import IndiceBM25


def parear(
    catalogo: Sequence[dict],
    itens: Sequence[dict],
    *,
    piso: float,
    top_k: int | None = None,
    embed: Callable[[list[str]], Any] | None = None,
    cfg: dict | None = None,
    on_categoria: Callable[[str, int, int], None] | None = None,
) -> list[dict]:
    """`catalogo`: `[{codigo, texto, categoria}]` · `itens`: `[{item_key, descricao_final,
    categoria}]` → `[{par_key, codigo, item_key, categoria, score_bm25, score_cosseno}]`.

    `embed` é injetado para o servidor usar a GPU dele e o adapter em processo usar o embedder
    local. Sem `embed`, o cosseno é zero e sobra o BM25 — que é exatamente o comportamento do
    `--sem-embedding` da etapa 6a, útil para rodar sem GPU nenhuma.
    """
    import numpy as np

    if embed is None and cfg is not None:
        embed = _embedder_de_cfg(cfg)

    por_categoria: dict[str, dict[str, list]] = {}
    for c in catalogo:
        por_categoria.setdefault(c["categoria"], {"cat": [], "itens": []})["cat"].append(c)
    for i in itens:
        if i["categoria"] in por_categoria:
            por_categoria[i["categoria"]]["itens"].append(i)

    pares: list[dict] = []
    for categoria, grupo in por_categoria.items():
        c_cat, i_cat = grupo["cat"], grupo["itens"]
        if not c_cat or not i_cat:
            continue

        corpus = [str(i.get("descricao_final") or "") for i in i_cat]
        textos_cat = [str(c.get("texto") or "") for c in c_cat]
        indice = IndiceBM25(corpus)

        bm = np.vstack([indice.pontuar(t) for t in textos_cat])          # (n_cod, n_itens)
        bmin, bmax = bm.min(), bm.max()
        # Normalização DENTRO da categoria: escalas de BM25 não são comparáveis entre corpora
        # de tamanhos diferentes, e um piso global sobre valores crus cortaria categorias
        # pequenas inteiras.
        bm_norm = (bm - bmin) / (bmax - bmin) if bmax > bmin else np.zeros_like(bm)

        if embed is not None:
            emb_cat = embed(textos_cat)
            emb_itn = embed(corpus)
            cos = np.asarray(emb_cat) @ np.asarray(emb_itn).T
        else:
            cos = np.zeros_like(bm)

        item_keys = [i["item_key"] for i in i_cat]
        vivos = 0
        for ci, c in enumerate(c_cat):
            codigo = c["codigo"]
            scores = np.maximum(bm_norm[ci], cos[ci])       # basta UM sinal dizer "pode ser"
            idx = np.where(scores >= piso)[0]
            if idx.size == 0:
                continue
            # `kind="stable"` reproduz o desempate do `rank(method="first")` original: sem
            # isso, dois pares de score idêntico trocariam de posição entre execuções e o
            # top-K devolveria conjuntos diferentes para a mesma entrada.
            ordem = idx[np.argsort(-scores[idx], kind="stable")]
            top = ordem[:top_k] if top_k else ordem
            for ii in top:
                pares.append({
                    "par_key": f"{codigo}::{item_keys[ii]}",
                    "codigo": codigo,
                    "item_key": item_keys[ii],
                    "categoria": categoria,
                    "score_bm25": round(float(bm_norm[ci, ii]), 4),
                    "score_cosseno": round(float(cos[ci, ii]), 4),
                })
                vivos += 1
        if on_categoria is not None:
            on_categoria(categoria, len(c_cat) * len(i_cat), vivos)
    return pares


def _embedder_de_cfg(cfg: dict):
    """Embedder em processo a partir do `.env` — só o adapter em processo passa por aqui.

    Import tardio de propósito: `sentence-transformers` é dependência OPCIONAL desde a
    Fase 11 (`.[localmente]`), e o servidor que não usa embedding não pode ser obrigado a tê-la.
    """
    from pesquisa_precos.providers.embedder_local import EmbedderLocal

    cliente = EmbedderLocal(cfg.get("embedder_model", "BAAI/bge-m3"))
    return cliente.embed_textos
