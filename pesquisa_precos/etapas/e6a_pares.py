"""
Etapa 6a — Geração de pares (catálogo × item, mesma categoria) + rejeitor híbrido BM25+embedding.

Produto (codigo_catalogo × item_pncp) RESTRITO à mesma categoria; item multi-label pareia em
todas as suas categorias; SEM dedup de pares (regra de negócio). Para cada par mede-se um
score léxico (BM25 normalizado por categoria) e um score semântico (cosseno bge-m3). O par
sobrevive se max(bm25_norm, cosseno) >= REJEITOR_THRESHOLD — basta um sinal dizer "pode ser".

Entradas: data/4_itens_sobreviventes.csv, data/5_itens_enriquecidos.csv (opcional),
          data/0a_catalogo_filtrado.csv (+ categoria do código via etapa 1).
Saída: data/6a_pares_candidatos.csv (par_key, codigo, item_key, categoria, score_bm25,
       score_cosseno, sobreviveu). Rejeitados ficam com sobreviveu=false (auditoria).
GPU: o embedder roda sozinho. Embeddings são cacheados por hash de texto em
data/checkpoints/6a_emb_cache.parquet — numa atualização só os textos NOVOS (itens/códigos
novos) vão à GPU; o BM25 e o corte são recomputados frescos (baratos, na CPU).
Uso: python -m pesquisa_precos.etapas.e6a_pares [--sem-embedding]
"""

import argparse
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import numpy as np
import pandas as pd
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from pesquisa_precos.config import paths
from pesquisa_precos.config.settings import carregar_config
from pesquisa_precos.core.pareamento.indice_lexical import IndiceBM25
from pesquisa_precos.core.io_seguro import ler_csv

SOBREVIVENTES = paths.E4_SOBREVIVENTES
ENRIQUECIDOS = paths.E5_ENRIQUECIDOS
DESTINO = paths.E5_DESTINO
CATALOGO = paths.E0A_CATALOGO
CATEGORIA_POR_CODIGO = paths.E1_CATEGORIA_POR_CODIGO
EMB_CACHE = paths.CK_6A_EMB_CACHE  # embeddings por hash de texto (reuso entre rodadas)
SAIDA = paths.E6A_PARES


def mapa_codigo_categoria() -> dict[str, str]:
    """codigo → categoria, de 1_categoria_por_codigo.csv (fonte canônica per-item da etapa 1)."""
    mapa: dict[str, str] = {}
    if CATEGORIA_POR_CODIGO.exists():
        for r in ler_csv(str(CATEGORIA_POR_CODIGO)):
            if r.get("codigo") and r.get("categoria"):
                mapa.setdefault(r["codigo"], r["categoria"])
    if not mapa:
        raise SystemExit(f"Sem mapa código→categoria ({CATEGORIA_POR_CODIGO}). Rode a etapa 1 antes.")
    return mapa


def carregar_itens() -> pd.DataFrame:
    if not SOBREVIVENTES.exists():
        raise SystemExit(f"{SOBREVIVENTES} ausente. Rode a etapa 4 antes.")
    itens = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    itens["descricao_final"] = itens.get("descricao_api", "")
    if ENRIQUECIDOS.exists():
        enr = pd.read_csv(ENRIQUECIDOS, dtype=str, encoding="utf-8").fillna("")
        m = dict(zip(enr["item_key"], enr["descricao_final"]))
        itens["descricao_final"] = itens.apply(
            lambda r: m.get(r["item_key"]) or r["descricao_final"], axis=1)
    # Comporta de enriquecimento: só parear itens 'manter' (descrição rica confirmada no PDF).
    # 'descartar' (falha isolada, descrição pobre) e 'revisar' (PDF trocado/ilegível → aba
    # separada) saem do fluxo de pareamento. Sem o arquivo de destino, mantém tudo (retrocompat).
    if DESTINO.exists():
        dst = {r["item_key"]: r.get("destino", "manter") for r in ler_csv(str(DESTINO))}
        antes = len(itens)
        itens = itens[itens["item_key"].map(lambda k: dst.get(k, "manter")) == "manter"].copy()
        print(f"[6a] Comporta de enriquecimento: {antes} → {len(itens)} itens (só 'manter').")
    return itens


def aplicar_corte(df, top_k: int, piso: float):
    """
    Define `sobreviveu` = (score_efetivo >= piso) E (está entre os top-K por código).

    Motivo: dentro de uma categoria homogênea (viatura, equip_ti) quase todo par passa de
    um limiar global baixo — o produto cartesiano explode (dezenas de milhões) e afoga o
    reranker. O top-K por código mantém, para cada item do catálogo, só os K candidatos
    PNCP mais similares; o piso remove a cauda de ruído puro. Imprime o que foi cortado.
    """
    if df.empty:
        return df
    n0 = len(df)
    df["score_ef"] = df[["score_bm25", "score_cosseno"]].astype(float).max(axis=1)
    passa_piso = df["score_ef"] >= piso
    # rank por código só entre quem passou do piso (rank denso, maior score = 1).
    df["rank_cod"] = (df[passa_piso].groupby("codigo")["score_ef"]
                      .rank(method="first", ascending=False))
    df["sobreviveu"] = passa_piso & (df["rank_cod"] <= top_k)
    vivos = int(df["sobreviveu"].sum())
    cort_piso = int((~passa_piso).sum())
    cort_topk = int((passa_piso & (df["rank_cod"] > top_k)).sum())
    print(f"[6a] CORTE aplicado (top-K={top_k}, piso={piso}):")
    print(f"     {n0} pares → {vivos} sobreviventes "
          f"({100*vivos/n0:.1f}%). Cortados: {cort_piso} pelo piso + {cort_topk} pelo top-K.")
    # Resumo por categoria (antes → depois) para as maiores.
    antes = df.groupby("categoria").size()
    depois = df[df["sobreviveu"]].groupby("categoria").size()
    for cat in antes.sort_values(ascending=False).index[:6]:
        print(f"     {cat:<26} {int(antes[cat]):>10} → {int(depois.get(cat, 0)):>8}")
    return df.drop(columns=["score_ef", "rank_cod"])


def refiltar_streaming(caminho: str, top_k: int, piso: float):
    """
    Reaplica top-K+piso num 6a_pares_candidatos.csv já gerado, SEM recomputar embeddings e
    SEM carregar tudo na memória (2 passes em streaming). Sobrescreve o arquivo via temp.

    Pass 1: por código, guarda os K maiores score_ef num heap → cutoff[código] = K-ésimo maior.
    Pass 2: sobreviveu = (score_ef >= piso) E (score_ef >= cutoff[código]). Grava e reporta.
    """
    import csv
    import heapq
    import os
    from collections import Counter, defaultdict

    csv.field_size_limit(10 * 1024 * 1024)

    def score_ef(row):
        try:
            return max(float(row["score_bm25"]), float(row["score_cosseno"]))
        except (ValueError, KeyError):
            return 0.0

    print(f"[6a] Refiltrando {caminho} (streaming, sem recomputar embeddings)…")
    print("[6a] pass 1/2: apurando o corte por código…")
    heaps: dict[str, list] = defaultdict(list)  # código → min-heap dos K maiores score_ef
    with open(caminho, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        campos = reader.fieldnames
        for row in reader:
            s = score_ef(row)
            if s < piso:
                continue
            h = heaps[row["codigo"]]
            if len(h) < top_k:
                heapq.heappush(h, s)
            elif s > h[0]:
                heapq.heapreplace(h, s)
    cutoff = {cod: (h[0] if len(h) >= top_k else piso) for cod, h in heaps.items()}

    print("[6a] pass 2/2: reescrevendo o arquivo…")
    tmp = caminho + ".tmp"
    n0 = vivos = cort_piso = cort_topk = 0
    antes = Counter(); depois = Counter()
    with open(caminho, "r", encoding="utf-8", newline="") as fin, \
         open(tmp, "w", encoding="utf-8", newline="") as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=campos)
        writer.writeheader()
        for row in reader:
            n0 += 1
            cat = row.get("categoria", "")
            antes[cat] += 1
            s = score_ef(row)
            if s < piso:
                sobrev = False; cort_piso += 1
            elif s >= cutoff.get(row["codigo"], piso):
                sobrev = True
            else:
                sobrev = False; cort_topk += 1
            if not sobrev:
                continue  # grava só sobreviventes: o arquivo final fica com poucos MB.
            vivos += 1; depois[cat] += 1
            row["sobreviveu"] = True
            writer.writerow(row)
    os.replace(tmp, caminho)

    print(f"[6a] CORTE aplicado (top-K={top_k}, piso={piso}):")
    print(f"     {n0} pares → {vivos} sobreviventes ({100*vivos/max(n0,1):.1f}%). "
          f"Cortados: {cort_piso} pelo piso + {cort_topk} pelo top-K.")
    for cat in sorted(antes, key=lambda c: -antes[c])[:6]:
        print(f"     {cat:<26} {antes[cat]:>10} → {depois.get(cat, 0):>8}")
    print(f"[6a] Refiltro concluído → {caminho}")


def main():
    ap = argparse.ArgumentParser(description="Etapa 6a — pares + rejeitor híbrido")
    ap.add_argument("--sem-embedding", action="store_true", help="Só BM25 (pula o embedder/GPU)")
    ap.add_argument("--remoto", action="store_true",
                    help="usa o servidor de GPU (GPU_BASE_URL) para o embedder, em vez do local")
    ap.add_argument("--top-k", type=int, default=100,
                    help="por código, mantém só os K itens PNCP mais similares (default 100)")
    ap.add_argument("--piso", type=float, default=0.40,
                    help="score efetivo mínimo (max bm25/cosseno) p/ par sobreviver (default 0.40)")
    ap.add_argument("--refiltar", action="store_true",
                    help="reaplica top-K+piso no 6a_pares_candidatos.csv já gerado (NÃO recomputa embeddings)")
    args = ap.parse_args()

    if args.refiltar:
        if not SAIDA.exists():
            raise SystemExit(f"{SAIDA} ausente — nada a refiltrar. Rode a 6a normal antes.")
        refiltar_streaming(str(SAIDA), args.top_k, args.piso)
        return

    cfg = carregar_config()

    cod_cat = mapa_codigo_categoria()
    cat = pd.read_csv(CATALOGO, dtype=str, encoding="utf-8-sig").fillna("")
    cat["categoria"] = cat["codigo"].map(cod_cat).fillna("")
    cat = cat[cat["categoria"] != ""].copy()
    cat["texto"] = (cat["nome_pdm"] + " " + cat["descricao"]).str.strip()

    itens = carregar_itens()
    itens = itens.assign(categoria=itens["categorias"].str.split("|")).explode("categoria")
    itens = itens[itens["categoria"].str.strip() != ""].copy()

    categorias = sorted(set(cat["categoria"]) & set(itens["categoria"]))
    print(f"[6a] Categorias com catálogo e itens: {categorias}")

    # Cache de embeddings por texto (sha1): numa atualização, textos de catálogo/itens já vistos
    # vêm do cache e só os NOVOS vão à GPU. Exato (não sofre o drift do min-max do BM25, que é
    # recomputado fresco). Chave = texto, então código/item inalterado ⇒ acerto de cache.
    embedder = None
    if not args.sem_embedding:
        if args.remoto:
            from pesquisa_precos.providers.gpu_remoto import EmbedderRemoto
            embedder = EmbedderRemoto(cfg["gpu_base_url"], cfg["gpu_api_key"], cache_path=str(EMB_CACHE))
            print(f"[6a] embedder remoto: {cfg['gpu_base_url']}")
        else:
            from pesquisa_precos.providers.embedder_local import EmbedderLocal
            embedder = EmbedderLocal(cfg["embedder_model"], cache_path=str(EMB_CACHE))

    linhas = []
    total_pares = cort_piso = cort_topk = 0   # relatório do corte (streaming)
    antes_cat: dict[str, int] = {}
    depois_cat: dict[str, int] = {}
    progress = Progress(
        SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=30),
        MofNCompleteColumn(), TimeElapsedColumn(), console=Console(),
    )
    progress.start()
    tarefa = progress.add_task("gerando pares", total=len(categorias))
    for categoria in categorias:
        progress.update(tarefa, description=f"gerando pares · {categoria}")
        c_cat = cat[cat["categoria"] == categoria].reset_index(drop=True)
        i_cat = itens[itens["categoria"] == categoria].reset_index(drop=True)
        if c_cat.empty or i_cat.empty:
            progress.advance(tarefa)
            continue
        corpus = i_cat["descricao_final"].tolist()
        indice = IndiceBM25(corpus)

        # BM25: para cada código, score contra todos os itens; normaliza min-max por categoria.
        bm = np.vstack([indice.pontuar(t) for t in c_cat["texto"]])  # (n_cod, n_itens)
        bmin, bmax = bm.min(), bm.max()
        bm_norm = (bm - bmin) / (bmax - bmin) if bmax > bmin else np.zeros_like(bm)

        if embedder is not None:
            from pesquisa_precos.providers.embedder_local import cosseno_linhas  # noqa: F811
            emb_cat = embedder.embed_textos(c_cat["texto"].tolist())      # (n_cod, d)
            emb_itn = embedder.embed_textos(corpus)                       # (n_itens, d)
            cos = emb_cat @ emb_itn.T                                     # (n_cod, n_itens)
        else:
            cos = np.zeros_like(bm)

        # Corte em STREAMING por código (top-K + piso), guardando SÓ os sobreviventes: evita
        # materializar o produto cartesiano inteiro (dezenas de milhões de pares) na memória.
        # Equivalente ao antigo aplicar_corte: score_ef = max(bm,cos); por código, os top-K
        # acima do piso; desempate estável pela ordem do item (= rank method="first").
        n_pares_cat = len(c_cat) * len(i_cat)
        total_pares += n_pares_cat
        item_keys = i_cat["item_key"].tolist()
        vivos_cat = 0
        for ci in range(len(c_cat)):
            codigo = c_cat.at[ci, "codigo"]
            scores = np.maximum(bm_norm[ci], cos[ci])          # score_ef por item
            idx = np.where(scores >= args.piso)[0]             # passam o piso
            cort_piso += len(i_cat) - idx.size
            if idx.size == 0:
                continue
            ordem = idx[np.argsort(-scores[idx], kind="stable")]  # top-K por score (desc, estável)
            top = ordem[: args.top_k]
            cort_topk += idx.size - len(top)
            for ii in top:
                linhas.append({
                    "par_key": f"{codigo}::{item_keys[ii]}",
                    "codigo": codigo,
                    "item_key": item_keys[ii],
                    "categoria": categoria,
                    "score_bm25": round(float(bm_norm[ci, ii]), 4),
                    "score_cosseno": round(float(cos[ci, ii]), 4),
                    "sobreviveu": True,
                })
                vivos_cat += 1
        antes_cat[categoria] = n_pares_cat
        depois_cat[categoria] = vivos_cat
        progress.console.print(
            f"  {categoria}: {len(c_cat)} cód × {len(i_cat)} itens = {n_pares_cat} pares "
            f"→ {vivos_cat} sobreviventes")
        progress.advance(tarefa)
    progress.stop()

    if embedder is not None:
        embedder.salvar_cache()  # persiste os embeddings novos p/ a próxima rodada reusar
        embedder.liberar()

    df = pd.DataFrame(linhas, columns=["par_key", "codigo", "item_key", "categoria",
                                       "score_bm25", "score_cosseno", "sobreviveu"])
    df.to_csv(SAIDA, index=False, encoding="utf-8")
    print(f"[6a] CORTE aplicado em streaming (top-K={args.top_k}, piso={args.piso}):")
    print(f"     {total_pares} pares → {len(df)} sobreviventes "
          f"({100*len(df)/max(total_pares,1):.1f}%). "
          f"Cortados: {cort_piso} pelo piso + {cort_topk} pelo top-K.")
    for cat in sorted(antes_cat, key=lambda c: -antes_cat[c])[:6]:
        print(f"     {cat:<26} {antes_cat[cat]:>10} → {depois_cat.get(cat, 0):>8}")
    print(f"[6a] Pares gravados: {len(df)} | saída: {SAIDA}")


if __name__ == "__main__":
    main()
