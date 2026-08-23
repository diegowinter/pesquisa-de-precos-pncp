"""
Etapa 1 — Termos de busca genéricos por item, mais a categoria de cada um, a partir do
catálogo filtrado pela etapa 0a.

Os termos saem direto de cada item (`nome_pdm` + descrição), o mais genéricos possível, e são
agregados um termo por linha, juntando os códigos de catálogo que o pediram. A alternativa —
agrupar itens num "conceito" e pedir sinônimos dele — puxava termos de objetos diferentes.

Fluxo:
  1. por item, no LLM (paralelo, um cliente por thread): termos genéricos e categoria;
  2. categoria nunca fica vazia: LLM, depois maioria dentro do mesmo `codigo_pdm`, depois
     maioria do mesmo conjunto de termos, depois o mapa de `nome_grupo`, e por fim "outros";
  3. expande variações de grafia e duplica a forma sem acento;
  4. agrega um termo por linha: termo -> união dos códigos de catálogo.

Entrada: `catalogo_item`. Saídas: `termo_geracao` (a resposta bruta do LLM, que também é o
checkpoint por item), `termo`/`termo_codigo` (o agregado) e `catalogo_item.categoria`.

Não descartar as linhas com origem manual: são curadoria humana e sobrevivem à regeração.
"""

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
from sqlalchemy import text as sa_text
from pydantic import BaseModel, Field

from pesquisa_precos.core.classification.variations import (
    categoria_por_grupo,
    e_generico,
    expandir_variacoes,
)
from pesquisa_precos.core.parallel import executar_paralelo
from pesquisa_precos.steps.base import RunContext, Estimate, StepResult

KEY = "1"
# 2.0.0 (Fase 13): sobrou só o banco. A regra (cascata de categoria, expansão de
# variações, agregação por termo) é a MESMA — só muda de onde vem o catálogo e para
# onde vão termos/categoria. `termo_geracao` é o checkpoint por item.
CODE_VERSION = "2.0.0"


CAT_FALLBACK = "outros"


class Params(BaseModel):
    provider: str = Field("local", description="Provedor de LLM [local|openrouter]")
    limite: int | None = Field(None, description="Processa só N itens do catálogo (teste)")
    concurrency: int = Field(3, ge=1, le=32, description="Chamadas simultâneas ao LLM")
    forte: bool = Field(False, description="Usa o model PASS2 (caro). Só afeta 'openrouter'.")
    regerar: bool = Field(False, description="Recria a saída do zero (pede confirmação)")


def _norm(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (s or "").strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


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


def agregar_por_termo(df, checkpoint, categorias, ctx: RunContext) -> list[dict]:
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
            "source": "llm",
        })
    return linhas


# ── Geração gravando no banco (Fase 10) ─────────────────────────────────────────────
#
# Só a BORDA muda: o catálogo vem de `catalogo_item` em vez do CSV filtrado, e o resultado vai
# para `termo`/`termo_codigo`/`catalogo_item.categoria` em vez de dois CSVs. O miolo —
# `resolver_categorias`, `expandir_termos`, `agregar_por_termo` — é literalmente o mesmo
# código, porque o DataFrame do banco sai com os MESMOS nomes de coluna (mesma estratégia da
# etapa 7 na Fase 2).

def _exigir_banco():
    from pesquisa_precos.db import session as db

    ok, detalhe = db.is_available()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
    return db


def carregar_catalogo_do_banco() -> pd.DataFrame:
    """`catalogo_item` ativo com as colunas que o resto da etapa espera do CSV."""
    db = _exigir_banco()
    with db.session() as s:
        linhas = s.execute(sa_text(
            "SELECT tipo::text, codigo, coalesce(codigo_pdm, '') AS codigo_pdm, "
            "       coalesce(nome_pdm, '') AS nome_pdm, coalesce(descricao, '') AS descricao, "
            "       coalesce(nome_grupo, '') AS nome_grupo "
            "  FROM catalogo_item WHERE active ORDER BY tipo, codigo")).all()
    if not linhas:
        raise SystemExit("catalogo_item vazio — rode a etapa 0a antes (ou revise a "
                         "allow-list em pdm_permitido).")
    return pd.DataFrame(linhas, columns=["tipo", "codigo", "codigo_pdm", "nome_pdm",
                                         "descricao", "nome_grupo"])


def gerar_por_item_no_banco(df, criar_curador, concurrency, params,
                            ctx: RunContext) -> tuple[int, int]:
    """Mesmo laço paralelo do caminho CSV; o `EscritorSeguro` dá lugar a `termo_geracao`.

    Cada item é gravado na sua PRÓPRIA transação, assim que volta do LLM — é o que preserva a
    propriedade que o checkpoint em CSV dava: processo morto no meio não perde (nem repete) as
    chamadas já pagas.
    """
    db = _exigir_banco()
    from pesquisa_precos.db.repos import termo as repo_termo

    with db.session() as s:
        feitas = repo_termo.codigos_ja_gerados(s)
    pendentes = [r for _, r in df.iterrows() if (r["tipo"], r["codigo"]) not in feitas]
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

    n_ok, n_erros = [0], [0]
    model = "PASS2" if params.forte else "PASS1"

    def fn(row):
        cur = _curador()
        termos = cur.gerar_termos_item(row.get("nome_pdm", ""), row.get("descricao", ""),
                                       row.get("tipo", ""), row.get("nome_grupo", ""))
        cats = cur.classificar_categoria(row.get("descricao", ""))["categorias"]
        return {"termos": termos, "categoria": cats[0] if cats else ""}

    def ok(row, res):
        n_ok[0] += 1
        with db.session() as s:
            repo_termo.gravar_geracao(s, row["tipo"], row["codigo"], res["termos"],
                                      res["categoria"], model=model,
                                      provider=params.provedor)
            s.commit()

    def err(row, exc):
        n_erros[0] += 1
        ctx.log("erro", f"[red]erro[/] {row.get('tipo')}/{row.get('codigo')}: {exc}")
        ctx.erro_item(str(row.get("codigo")), exc, tipo=str(row.get("tipo")),
                      name=str(row.get("nome_pdm")))

    ctx.progresso(0, len(pendentes), descricao="itens")
    executar_paralelo(pendentes, fn, concurrency=concurrency, on_result=ok,
                      on_error=err, on_progress=lambda f, t: ctx.progresso(f, t))
    if n_erros[0]:
        ctx.log("aviso", f"[yellow][1] {n_erros[0]} itens falharam — ver item_error[/]")
    return n_ok[0], n_erros[0]


def gravar_termos_no_banco(linhas: list[dict], ctx: RunContext) -> dict:
    """Agregado da etapa (um termo por linha) → `termo` + `termo_codigo`.

    `source='manual'` não é tocado em nenhum momento: é curadoria humana, e o caminho CSV já
    a preservava na regeração. Os termos de LLM que deixaram de ser gerados são DESATIVADOS
    (não apagados) — ver `repo_termo.desativar_llm_ausentes`.
    """
    db = _exigir_banco()
    from pesquisa_precos.core.text import normalizar_termo
    from pesquisa_precos.db.repos import termo as repo_termo

    ligacoes = 0
    norms: list[str] = []
    with db.session() as s:
        for linha in linhas:
            termo_id = repo_termo.upsert(s, linha["termos"], linha["categoria"], "llm")
            if termo_id is None:
                continue
            norms.append(normalizar_termo(linha["termos"]))
            codigos = [c for c in linha["codigos_catalogo"].split("|") if c]
            # O par (tipo, codigo) não vem do agregado (que junta códigos de tipos diferentes
            # sob o mesmo termo); resolvê-lo aqui evita carregar o catálogo de novo.
            pares = s.execute(sa_text(
                "SELECT tipo::text, codigo FROM catalogo_item WHERE codigo = ANY(:c)"),
                {"c": codigos}).all()
            ligacoes += repo_termo.ligar_codigos(s, termo_id, [(t, c) for t, c in pares])
        desativados = repo_termo.desativar_llm_ausentes(s, norms)
        s.commit()
        n_termos, n_ligacoes, _ = repo_termo.contar(s)
    if desativados:
        ctx.log("info", f"[dim][1] {desativados} termos de LLM não regerados foram "
                        f"desativados (manuais intactos).[/]")
    return {"termos_no_banco": n_termos, "ligacoes_termo_codigo": n_ligacoes,
            "ligacoes_gravadas": ligacoes, "termos_desativados": desativados}


def run(params: Params, ctx: RunContext) -> StepResult:
    db = _exigir_banco()
    from pesquisa_precos.db.repos import termo as repo_termo

    df = carregar_catalogo_do_banco()
    if params.limite:
        df = df.head(params.limite)

    def criar_curador():
        return ctx.providers.novo_chat(curador_kwargs={"max_retries": 6}).curador

    ctx.log("debug", f"[dim][1] Provedor: {params.provedor} · model: "
                     f"{'PASS2 (forte)' if params.forte else 'PASS1'} · fonte: banco[/]")
    n_ok, n_erros = gerar_por_item_no_banco(df, criar_curador, params.concurrency, params, ctx)

    with db.session() as s:
        checkpoint = repo_termo.geracoes(s)

    categorias = resolver_categorias(df, checkpoint)
    with db.session() as s:
        alteradas = repo_termo.gravar_categorias(s, categorias)
        s.commit()
    ctx.log("info", f"[bold green][1] Categoria gravada[/] em catalogo_item "
                    f"([bold]{len(categorias)}[/] códigos, {alteradas} alterados).")

    novas = agregar_por_termo(df, checkpoint, categorias, ctx)
    metrics = gravar_termos_no_banco(novas, ctx)
    ctx.log("info", f"[bold green][1] Gravado[/] [bold]{len(novas)}[/] termos "
                    f"({metrics['termos_no_banco']} no banco, incluindo manuais).")

    return StepResult(
        processed=n_ok, erros=n_erros,
        metrics={"itens_do_catalogo": len(df), "termos_gerados": len(novas), **metrics},
        preview=novas[:50],
    )


def estimate(params: Params, ctx: RunContext) -> Estimate:
    """Uma chamada de LLM por código de catálogo ainda sem termos no checkpoint."""
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import termo as repo_termo

    ok, detalhe = db.is_available()
    if not ok:
        return Estimate(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    try:
        df = carregar_catalogo_do_banco()
    except SystemExit as e:
        # Estimar NUNCA aborta: é o comando que o operador roda justamente para descobrir
        # se dá para rodar. Catálogo vazio é um aviso, do mesmo jeito que o CSV ausente é
        # no caminho legado.
        return Estimate(detalhes={"aviso": str(e)})
    if params.limite:
        df = df.head(params.limite)
    with db.session() as s:
        feitas = repo_termo.codigos_ja_gerados(s)
    n = sum(1 for _, r in df.iterrows() if (r["tipo"], r["codigo"]) not in feitas)
    resolucao = ctx.providers.resolucao_opcional("chat")
    preco = resolucao.info.cost_usd_per_call if resolucao else None
    return Estimate(
        unidades=n, chamadas_llm=n,
        cost_usd=None if preco is None else n * preco,
        duracao_s=n / max(params.concurrency, 1) * 2,
        detalhes={"codigos_no_catalogo": len(df),
                  "já_feitos": len(feitas)},
    )
