"""
Etapa 6a — Geração de pares (catálogo × item, mesma categoria) + rejeitor híbrido BM25+embedding.

Produto (codigo_catalogo × item_pncp) RESTRITO à mesma categoria; item multi-label pareia em
todas as suas categorias; SEM dedup de pares (regra de negócio). Para cada par mede-se um
score léxico (BM25 normalizado por categoria) e um score semântico (cosseno bge-m3). O par
sobrevive se max(bm25_norm, cosseno) >= piso — basta um sinal dizer "pode ser".

Entradas: data/4_itens_sobreviventes.csv, data/5_itens_enriquecidos.csv (opcional),
          data/0a_catalogo_filtrado.csv (+ categoria do código via etapa 1).
Saída: data/6a_pares_candidatos.csv (par_key, codigo, item_key, categoria, score_bm25,
       score_cosseno, sobreviveu). Chave de resumo: nenhuma — recomputa o corpus inteiro.
GPU: o embedder roda sozinho. Embeddings são cacheados por hash de texto em
data/checkpoints/6a_emb_cache.parquet — numa atualização só os textos NOVOS (itens/códigos
novos) vão à GPU; o BM25 e o corte são recomputados frescos (baratos, na CPU).

⚠ NÃO fazer: aplicar o corte top-K + piso DEPOIS de materializar o produto cartesiano num
DataFrame. Um `aplicar_corte` pós-hoc com groupby().rank() já causou MemoryError real com
~33M linhas; o corte é feito em streaming, por código, direto nas matrizes numpy. O arquivo
data/6a_pares_candidatos_PRECORTE.csv (3,7 GB) é o fóssil desse bug.

Uso: python -m pesquisa_precos.etapas.e6a_pares [--sem-embedding] [--remoto] [--refiltar]
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from pesquisa_precos.config import paths
from pesquisa_precos.core.io_seguro import ler_csv
from pesquisa_precos.core.pareamento.indice_lexical import IndiceBM25
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa

CHAVE = "6a"
VERSAO_CODIGO = "1.0.0"

SOBREVIVENTES = paths.E4_SOBREVIVENTES
ENRIQUECIDOS = paths.E5_ENRIQUECIDOS
DESTINO = paths.E5_DESTINO
CATALOGO = paths.E0A_CATALOGO
CATEGORIA_POR_CODIGO = paths.E1_CATEGORIA_POR_CODIGO
EMB_CACHE = paths.CK_6A_EMB_CACHE  # embeddings por hash de texto (reuso entre rodadas)
SAIDA = paths.E6A_PARES


class Params(BaseModel):
    sem_embedding: bool = Field(False, description="Só BM25 (pula o embedder/GPU)")
    remoto: bool = Field(
        False, description="Usa o servidor de GPU (GPU_BASE_URL) para o embedder")
    top_k: int = Field(
        100, ge=1, description="Por código, mantém só os K itens PNCP mais similares")
    piso: float = Field(
        0.40, ge=0.0, le=1.0,
        description="Score efetivo mínimo (max bm25/cosseno) p/ o par sobreviver")
    refiltar: bool = Field(
        False, description="Reaplica top-K+piso no CSV já gerado (NÃO recomputa embeddings)")


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


def carregar_itens(ctx: ContextoExecucao) -> pd.DataFrame:
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
        ctx.log("info", f"[6a] Comporta de enriquecimento: {antes} → {len(itens)} itens "
                        f"(só 'manter').")
    return itens


def refiltar_streaming(caminho: str, top_k: int, piso: float, ctx: ContextoExecucao) -> dict:
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

    ctx.log("info", f"[6a] Refiltrando {caminho} (streaming, sem recomputar embeddings)…")
    ctx.log("info", "[6a] pass 1/2: apurando o corte por código…")
    heaps: dict[str, list] = defaultdict(list)  # código → min-heap dos K maiores score_ef
    with open(caminho, encoding="utf-8", newline="") as f:
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

    ctx.log("info", "[6a] pass 2/2: reescrevendo o arquivo…")
    tmp = caminho + ".tmp"
    n0 = vivos = cort_piso = cort_topk = 0
    antes = Counter()
    depois = Counter()
    with open(caminho, encoding="utf-8", newline="") as fin, \
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
                sobrev = False
                cort_piso += 1
            elif s >= cutoff.get(row["codigo"], piso):
                sobrev = True
            else:
                sobrev = False
                cort_topk += 1
            if not sobrev:
                continue  # grava só sobreviventes: o arquivo final fica com poucos MB.
            vivos += 1
            depois[cat] += 1
            row["sobreviveu"] = True
            writer.writerow(row)
    os.replace(tmp, caminho)

    ctx.log("info", f"[6a] CORTE aplicado (top-K={top_k}, piso={piso}):")
    ctx.log("info", f"     {n0} pares → {vivos} sobreviventes ({100*vivos/max(n0,1):.1f}%). "
                    f"Cortados: {cort_piso} pelo piso + {cort_topk} pelo top-K.")
    for cat in sorted(antes, key=lambda c: -antes[c])[:6]:
        ctx.log("info", f"     {cat:<26} {antes[cat]:>10} → {depois.get(cat, 0):>8}")
    ctx.log("info", f"[6a] Refiltro concluído → {caminho}")
    return {"pares_lidos": n0, "sobreviventes": vivos,
            "cortados_piso": cort_piso, "cortados_topk": cort_topk}


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Custo é GPU/CPU, não token. A unidade útil é o par candidato (produto por categoria)."""
    if not (SOBREVIVENTES.exists() and CATALOGO.exists() and CATEGORIA_POR_CODIGO.exists()):
        return Estimativa(detalhes={"aviso": "faltam entradas (rode 0a/1/4 antes)."})
    cod_cat = mapa_codigo_categoria()
    cat = pd.read_csv(CATALOGO, dtype=str, encoding="utf-8-sig").fillna("")
    cat["categoria"] = cat["codigo"].map(cod_cat).fillna("")
    cat = cat[cat["categoria"] != ""]
    itens = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    itens = itens.assign(categoria=itens["categorias"].str.split("|")).explode("categoria")
    itens = itens[itens["categoria"].str.strip() != ""]
    n_cod = cat.groupby("categoria").size()
    n_itn = itens.groupby("categoria").size()
    comuns = n_cod.index.intersection(n_itn.index)
    pares = int((n_cod[comuns] * n_itn[comuns]).sum())
    teto = int((n_cod[comuns] * params.top_k).sum())
    return Estimativa(
        unidades=pares, chamadas_llm=0, custo_usd=0.0,
        detalhes={"categorias": len(comuns), "codigos": int(n_cod[comuns].sum()),
                  "itens_explodidos": int(n_itn[comuns].sum()),
                  "teto_de_sobreviventes (top-K)": teto,
                  "embedder": "desligado" if params.sem_embedding else
                              ("GPU remota" if params.remoto else "local")},
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    if params.refiltar:
        if not SAIDA.exists():
            raise SystemExit(f"{SAIDA} ausente — nada a refiltrar. Rode a 6a normal antes.")
        metricas = refiltar_streaming(str(SAIDA), params.top_k, params.piso, ctx)
        return ResultadoEtapa(processados=metricas["sobreviventes"], metricas=metricas)

    cfg = ctx.config

    cod_cat = mapa_codigo_categoria()
    cat = pd.read_csv(CATALOGO, dtype=str, encoding="utf-8-sig").fillna("")
    cat["categoria"] = cat["codigo"].map(cod_cat).fillna("")
    cat = cat[cat["categoria"] != ""].copy()
    cat["texto"] = (cat["nome_pdm"] + " " + cat["descricao"]).str.strip()

    itens = carregar_itens(ctx)
    itens = itens.assign(categoria=itens["categorias"].str.split("|")).explode("categoria")
    itens = itens[itens["categoria"].str.strip() != ""].copy()

    categorias = sorted(set(cat["categoria"]) & set(itens["categoria"]))
    ctx.log("info", f"[6a] Categorias com catálogo e itens: {categorias}")

    # Cache de embeddings por texto (sha1): numa atualização, textos de catálogo/itens já vistos
    # vêm do cache e só os NOVOS vão à GPU. Exato (não sofre o drift do min-max do BM25, que é
    # recomputado fresco). Chave = texto, então código/item inalterado ⇒ acerto de cache.
    embedder = None
    if not params.sem_embedding:
        if params.remoto:
            from pesquisa_precos.providers.gpu_remoto import EmbedderRemoto
            embedder = EmbedderRemoto(cfg["gpu_base_url"], cfg["gpu_api_key"],
                                      cache_path=str(EMB_CACHE))
            ctx.log("info", f"[6a] embedder remoto: {cfg['gpu_base_url']}")
        else:
            from pesquisa_precos.providers.embedder_local import EmbedderLocal
            embedder = EmbedderLocal(cfg["embedder_model"], cache_path=str(EMB_CACHE))

    linhas = []
    total_pares = cort_piso = cort_topk = 0   # relatório do corte (streaming)
    antes_cat: dict[str, int] = {}
    depois_cat: dict[str, int] = {}
    ctx.progresso(0, len(categorias), descricao="gerando pares")
    for i, categoria in enumerate(categorias):
        if ctx.cancelado():
            break
        ctx.progresso(i, descricao=f"gerando pares · {categoria}")
        c_cat = cat[cat["categoria"] == categoria].reset_index(drop=True)
        i_cat = itens[itens["categoria"] == categoria].reset_index(drop=True)
        if c_cat.empty or i_cat.empty:
            ctx.progresso(i + 1)
            continue
        corpus = i_cat["descricao_final"].tolist()
        indice = IndiceBM25(corpus)

        # BM25: para cada código, score contra todos os itens; normaliza min-max por categoria.
        bm = np.vstack([indice.pontuar(t) for t in c_cat["texto"]])  # (n_cod, n_itens)
        bmin, bmax = bm.min(), bm.max()
        bm_norm = (bm - bmin) / (bmax - bmin) if bmax > bmin else np.zeros_like(bm)

        if embedder is not None:
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
            idx = np.where(scores >= params.piso)[0]           # passam o piso
            cort_piso += len(i_cat) - idx.size
            if idx.size == 0:
                continue
            ordem = idx[np.argsort(-scores[idx], kind="stable")]  # top-K por score (desc, estável)
            top = ordem[: params.top_k]
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
        ctx.log("info", f"  {categoria}: {len(c_cat)} cód × {len(i_cat)} itens = "
                        f"{n_pares_cat} pares → {vivos_cat} sobreviventes")
        ctx.progresso(i + 1)

    if embedder is not None:
        embedder.salvar_cache()  # persiste os embeddings novos p/ a próxima rodada reusar
        embedder.liberar()

    df = pd.DataFrame(linhas, columns=["par_key", "codigo", "item_key", "categoria",
                                       "score_bm25", "score_cosseno", "sobreviveu"])
    df.to_csv(SAIDA, index=False, encoding="utf-8")
    ctx.log("info", f"[6a] CORTE aplicado em streaming (top-K={params.top_k}, "
                    f"piso={params.piso}):")
    ctx.log("info", f"     {total_pares} pares → {len(df)} sobreviventes "
                    f"({100*len(df)/max(total_pares,1):.1f}%). "
                    f"Cortados: {cort_piso} pelo piso + {cort_topk} pelo top-K.")
    for c in sorted(antes_cat, key=lambda k: -antes_cat[k])[:6]:
        ctx.log("info", f"     {c:<26} {antes_cat[c]:>10} → {depois_cat.get(c, 0):>8}")
    ctx.log("info", f"[6a] Pares gravados: {len(df)} | saída: {SAIDA}")

    return ResultadoEtapa(
        processados=len(df), erros=0,
        metricas={"pares_possiveis": total_pares, "sobreviventes": len(df),
                  "cortados_piso": cort_piso, "cortados_topk": cort_topk,
                  "categorias": len(categorias)},
        preview=linhas[:50],
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
