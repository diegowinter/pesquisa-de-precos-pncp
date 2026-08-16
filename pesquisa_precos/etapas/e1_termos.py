"""
Etapa 1 — Termos de busca GENÉRICOS por item + categoria, a partir do catálogo filtrado (0a).

Em vez de agrupar itens em "conceitos" e gerar sinônimos a partir de um conceito genérico
(que puxava termos de objetos diferentes), os termos saem DIRETO de cada item (`nome_pdm` +
`descricao`), o mais genéricos possível, e são agregados UM TERMO POR LINHA juntando os
códigos de catálogo que o pediram.

Fluxo:
  1. Por item (LLM, paralelo, cliente por thread): termos genéricos (gerar_termos_item) +
     categoria (classificar_categoria). Checkpoint resumível: checkpoints/1_termos_item.csv.
  2. Categoria nunca vazia: LLM → maioria dentro do mesmo codigo_pdm → maioria do mesmo
     conjunto de termos → mapa nome_grupo → "outros".
  3. Expande variações de grafia (core/classificacao/variacoes) + duplica forma sem acento.
  4. Agrega um-termo-por-linha: termo → união dos codigos_catalogo (categoria = maioria).

Entradas: data/0a_catalogo_filtrado.csv.
Saídas:
  data/1_conceitos_termos.csv    (conceito=termo, categoria, termos=termo, codigos_catalogo, origem)
  data/1_categoria_por_codigo.csv (tipo, codigo, categoria)  ← fonte canônica por-item p/ a etapa 6a
Chave de resumo: (tipo, codigo) em checkpoints/1_termos_item.csv. Erros: erros/1_erros.csv.

NÃO fazer: deixar categoria vazia (a cascata de fallback existe por isso) nem descartar as
linhas `origem=manual` da saída — elas são curadoria humana e são preservadas na regeração.

Uso: python -m pesquisa_precos.etapas.e1_termos [--provedor local|openrouter] [--forte]
                                                [--limite N] [--concurrency N] [--regerar]
"""

import os
import sys
import threading
import unicodedata
from collections import Counter, defaultdict

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from pydantic import BaseModel, Field

from pesquisa_precos.config import paths
from pesquisa_precos.config.settings import custo_por_chamada, exigir
from pesquisa_precos.core.classificacao.variacoes import (
    categoria_por_grupo,
    e_generico,
    expandir_variacoes,
)
from pesquisa_precos.core.io_seguro import (
    EscritorSeguro,
    escrever_csv,
    ler_chaves_concluidas,
    ler_csv,
)
from pesquisa_precos.core.paralelo import executar_paralelo
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa
from pesquisa_precos.providers.llm_curador import Curador

CHAVE = "1"
VERSAO_CODIGO = "1.0.0"

CATALOGO_FILTRADO = paths.E0A_CATALOGO
CK_TERMOS = paths.CK_1_TERMOS_ITEM
SAIDA = paths.E1_TERMOS
CATEGORIA_POR_CODIGO = paths.E1_CATEGORIA_POR_CODIGO
ERROS = paths.ERROS_1

COLS_SAIDA = ["conceito", "categoria", "termos", "codigos_catalogo", "origem"]
COLS_CATEGORIA = ["tipo", "codigo", "categoria"]
CAT_FALLBACK = "outros"


class Params(BaseModel):
    provedor: str = Field("local", description="Provedor de LLM [local|openrouter]")
    limite: int | None = Field(None, description="Processa só N itens do catálogo (teste)")
    concurrency: int = Field(3, ge=1, le=32, description="Chamadas simultâneas ao LLM")
    forte: bool = Field(False, description="Usa o modelo PASS2 (caro). Só afeta 'openrouter'.")
    regerar: bool = Field(False, description="Recria a saída do zero (pede confirmação)")


def _norm(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (s or "").strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def carregar_catalogo() -> pd.DataFrame:
    if not CATALOGO_FILTRADO.exists():
        raise SystemExit(f"Catálogo filtrado ausente: {CATALOGO_FILTRADO}. Rode 0a antes.")
    return pd.read_csv(CATALOGO_FILTRADO, dtype=str, encoding="utf-8-sig").fillna("")


def _pendentes(df: pd.DataFrame) -> tuple[list, set]:
    """Itens do catálogo ainda sem termos no checkpoint (+ o conjunto dos já feitos)."""
    feitas = ler_chaves_concluidas(str(CK_TERMOS), ("tipo", "codigo"))
    return [r for _, r in df.iterrows() if (r["tipo"], r["codigo"]) not in feitas], feitas


def gerar_por_item(df, criar_curador, concurrency, ctx: ContextoExecucao) -> tuple[int, int]:
    """Preenche checkpoints/1_termos_item.csv (tipo, codigo, termos, categoria) de forma resumível.

    Cada worker usa o SEU próprio Curador (cliente por thread): compartilhar um único cliente
    ChatOpenAI entre threads serializa as chamadas HTTP e mata a concorrência.
    """
    os.makedirs(CK_TERMOS.parent, exist_ok=True)
    pendentes, feitas = _pendentes(df)
    if not pendentes:
        ctx.log("debug", f"[dim][1] Termos/categoria: todos os {len(feitas)} itens já feitos — "
                         f"pulando.[/]")
        return 0, 0
    ctx.log("info", f"[bold][1] Gerando termos + categoria de {len(pendentes)} itens[/] "
                    f"(já feitos: {len(feitas)}, concorrência: {concurrency})")

    _tls = threading.local()

    def _curador():
        if not hasattr(_tls, "c"):
            _tls.c = criar_curador()
        return _tls.c

    n_erros = [0]
    n_ok = [0]
    with EscritorSeguro(str(CK_TERMOS), ["tipo", "codigo", "termos", "categoria"]) as w:
        def fn(row):
            cur = _curador()
            termos = cur.gerar_termos_item(row.get("nome_pdm", ""), row.get("descricao", ""),
                                           row.get("tipo", ""), row.get("nome_grupo", ""))
            cats = cur.classificar_categoria(row.get("descricao", ""))["categorias"]
            return {"termos": termos, "categoria": cats[0] if cats else ""}

        def ok(row, res):
            n_ok[0] += 1
            w.escrever({"tipo": row["tipo"], "codigo": row["codigo"],
                        "termos": "|".join(res["termos"]), "categoria": res["categoria"]})

        def err(row, exc):
            n_erros[0] += 1
            ctx.log("erro", f"[red]erro[/] {row.get('tipo')}/{row.get('codigo')}: {exc}")
            ctx.erro_item(str(row.get("codigo")), exc, tipo=str(row.get("tipo")),
                          nome=str(row.get("nome_pdm")))

        ctx.progresso(0, len(pendentes), descricao="itens")
        executar_paralelo(pendentes, fn, concurrency=concurrency, on_result=ok,
                          on_error=err, on_progress=lambda f, t: ctx.progresso(f, t))
    if n_erros[0]:
        ctx.log("aviso", f"[yellow][1] {n_erros[0]} itens falharam — ver {ERROS}[/]")
    return n_ok[0], n_erros[0]


def _ler_checkpoint() -> dict:
    """(tipo, codigo) → {'termos': [...], 'categoria': str} a partir do checkpoint."""
    dados = {}
    for r in ler_csv(str(CK_TERMOS)):
        termos = [t for t in r.get("termos", "").split("|") if t]
        dados[(r["tipo"], r["codigo"])] = {"termos": termos, "categoria": r.get("categoria", "")}
    return dados


def resolver_categorias(df, checkpoint) -> dict:
    """codigo → categoria, nunca vazia.

    LLM → maioria do codigo_pdm → maioria do MESMO conjunto de termos (itens de mesma
    natureza, ex.: serviços idênticos com descrição levemente diferente) → nome_grupo → 'outros'.
    """
    cat_llm = {r["codigo"]: checkpoint.get((r["tipo"], r["codigo"]), {}).get("categoria", "")
               for _, r in df.iterrows()}

    def _termset(r):
        info = checkpoint.get((r["tipo"], r["codigo"]))
        return frozenset(_norm(x) for x in (info["termos"] if info else []) if x)

    # maioria da categoria dentro de cada codigo_pdm e dentro de cada conjunto-de-termos idêntico
    por_pdm, por_termos = defaultdict(list), defaultdict(list)
    for _, r in df.iterrows():
        c = cat_llm.get(r["codigo"], "")
        if not c:
            continue
        pdm = r.get("codigo_pdm", "").strip()
        if pdm:
            por_pdm[pdm].append(c)
        ts = _termset(r)
        if ts:
            por_termos[ts].append(c)
    maioria_pdm = {k: Counter(v).most_common(1)[0][0] for k, v in por_pdm.items()}
    maioria_termos = {k: Counter(v).most_common(1)[0][0] for k, v in por_termos.items()}

    final = {}
    for _, r in df.iterrows():
        cod = r["codigo"]
        cat = cat_llm.get(cod, "")
        if not cat:
            cat = maioria_pdm.get(r.get("codigo_pdm", "").strip(), "")
        if not cat:
            cat = maioria_termos.get(_termset(r), "")
        if not cat:
            cat = categoria_por_grupo(r.get("nome_grupo", ""))
        final[cod] = cat or CAT_FALLBACK
    return final


def expandir_termos(termos: list[str]) -> set[str]:
    """Aplica variações de grafia, duplica sem acento e descarta genéricos transversais."""
    base = {t.strip().lower() for t in termos if t.strip() and not e_generico(t)}
    base = expandir_variacoes(base)
    return {t for termo in base for t in (termo, _norm(termo)) if t and not e_generico(t)}


def agregar_por_termo(df, checkpoint, categorias, ctx: ContextoExecucao) -> list[dict]:
    """Explode itens→termos e agrega: termo → união dos codigos (categoria = maioria).

    Itens com categoria == CAT_FALLBACK ('outros') são fora de escopo: não geram termos
    (não serão buscados no PNCP).
    """
    codigos = defaultdict(set)   # termo → {codigo}
    cats = defaultdict(list)     # termo → [categoria]
    pulados = 0
    for _, r in df.iterrows():
        info = checkpoint.get((r["tipo"], r["codigo"]))
        if not info:
            continue
        if categorias.get(r["codigo"], CAT_FALLBACK) == CAT_FALLBACK:
            pulados += 1
            continue
        for termo in expandir_termos(info["termos"]):
            codigos[termo].add(r["codigo"])
            cats[termo].append(categorias.get(r["codigo"], CAT_FALLBACK))
    if pulados:
        ctx.log("debug", f"[dim][1] {pulados} itens em '{CAT_FALLBACK}' (fora de escopo) "
                         f"pulados da busca.[/]")

    linhas = []
    for termo in sorted(codigos):
        categoria = Counter(cats[termo]).most_common(1)[0][0]
        linhas.append({
            "conceito": termo,
            "categoria": categoria,
            "termos": termo,
            "codigos_catalogo": "|".join(sorted(codigos[termo])),
            "origem": "llm",
        })
    return linhas


def mesclar_preservando_manual(novas: list[dict], ctx: ContextoExecucao) -> list[dict]:
    """Mantém as linhas origem=manual; reconstrói as origem=llm; não duplica termo já manual."""
    manuais = [l for l in ler_csv(str(SAIDA)) if l.get("origem") == "manual"]
    termos_manual = {_norm(l.get("termos", "")) for l in manuais}
    novas = [l for l in novas if _norm(l["termos"]) not in termos_manual]
    if manuais:
        ctx.log("debug", f"[dim][1] {len(manuais)} linhas origem=manual preservadas.[/]")
    return manuais + novas


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Uma chamada de LLM por código de catálogo ainda sem termos no checkpoint."""
    if not CATALOGO_FILTRADO.exists():
        return Estimativa(detalhes={"aviso": f"{CATALOGO_FILTRADO} ausente — rode a 0a antes."})
    df = carregar_catalogo()
    if params.limite:
        df = df.head(params.limite)
    pendentes, feitas = _pendentes(df)
    n = len(pendentes)
    preco = custo_por_chamada(ctx.config, params.provedor, forte=params.forte)
    return Estimativa(
        unidades=n, chamadas_llm=n,
        custo_usd=None if preco is None else n * preco,
        duracao_s=n / max(params.concurrency, 1) * 2,
        detalhes={"codigos_no_catalogo": len(df), "já_feitos": len(feitas)},
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    cfg = ctx.config
    msg = exigir(cfg, params.provedor)
    if msg:
        raise SystemExit(msg)

    if params.regerar and SAIDA.exists():
        resp = input(f"Apagar {SAIDA.name} e regerar do zero? (digite 'sim'): ")
        if resp.strip().lower() == "sim":
            SAIDA.unlink()
        else:
            raise SystemExit("Cancelado.")

    df = carregar_catalogo()
    if params.limite:
        df = df.head(params.limite)

    def criar_curador():
        return Curador.from_provedor(cfg, params.provedor, forte=params.forte, max_retries=6)

    modelo = "PASS2 (forte)" if params.forte else "PASS1"
    ctx.log("debug", f"[dim][1] Provedor: {params.provedor} · modelo: {modelo}[/]")
    n_ok, n_erros = gerar_por_item(df, criar_curador, params.concurrency, ctx)

    checkpoint = _ler_checkpoint()
    categorias = resolver_categorias(df, checkpoint)
    escrever_csv(str(CATEGORIA_POR_CODIGO), COLS_CATEGORIA,
                 [{"tipo": r["tipo"], "codigo": r["codigo"], "categoria": categorias[r["codigo"]]}
                  for _, r in df.iterrows()])
    ctx.log("info", f"[bold green][1] Gravado[/] {CATEGORIA_POR_CODIGO} "
                    f"([bold]{len(df)}[/] itens, categoria nunca vazia).")

    novas = agregar_por_termo(df, checkpoint, categorias, ctx)
    final = mesclar_preservando_manual(novas, ctx)
    escrever_csv(str(SAIDA), COLS_SAIDA, final)
    ctx.log("info", f"[bold green][1] Gravado[/] {SAIDA} ([bold]{len(final)}[/] termos).")

    return ResultadoEtapa(
        processados=n_ok, erros=n_erros,
        metricas={"itens_do_catalogo": len(df), "termos_gerados": len(final),
                  "manuais_preservados": sum(1 for l in final if l.get("origem") == "manual")},
        preview=final[:50],
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
