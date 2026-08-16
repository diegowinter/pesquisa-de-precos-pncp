"""
Etapa 1 — Termos de busca GENÉRICOS por item + categoria, a partir do catálogo filtrado (0a).

Reescrita: em vez de agrupar itens em "conceitos" e gerar sinônimos a partir de um conceito
genérico (que puxava termos de objetos diferentes), agora geramos os termos DIRETO de cada item
(`nome_pdm` + `descricao`), o mais genéricos possível, e agregamos UM TERMO POR LINHA juntando os
códigos de catálogo que o pediram.

Fluxo:
  1. Por item (LLM, paralelo, cliente por thread): termos genéricos (gerar_termos_item) +
     categoria (classificar_categoria). Checkpoint resumível: checkpoints/1_termos_item.csv.
  2. Categoria nunca vazia: LLM → maioria dentro do mesmo codigo_pdm → mapa nome_grupo → "outros".
  3. Expande variações de grafia (scripts/variacoes) + duplica forma sem acento.
  4. Agrega um-termo-por-linha: termo → união dos codigos_catalogo (categoria = maioria).

Saídas:
  data/1_conceitos_termos.csv    (conceito=termo, categoria, termos=termo, codigos_catalogo, origem)
  data/1_categoria_por_codigo.csv (tipo, codigo, categoria)  ← fonte canônica por-item p/ a etapa 6a
Uso: python 1_gerar_conceitos.py [--provedor local|openrouter] [--forte] [--limite N]
                                  [--concurrency N] [--regerar]
"""

import argparse
import os
import sys
import threading
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

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

from scripts import erros_log
from scripts.config import carregar_config, exigir
from scripts.io_seguro import EscritorSeguro, ler_chaves_concluidas, ler_csv, escrever_csv
from scripts.llm_curador import Curador
from scripts.paralelo import executar_paralelo
from scripts.variacoes import categoria_por_grupo, e_generico, expandir_variacoes

DATA = Path(__file__).resolve().parent / "data"
CATALOGO_FILTRADO = DATA / "0a_catalogo_filtrado.csv"
CK_TERMOS = DATA / "checkpoints" / "1_termos_item.csv"
SAIDA = DATA / "1_conceitos_termos.csv"
CATEGORIA_POR_CODIGO = DATA / "1_categoria_por_codigo.csv"
ERROS = DATA / "erros" / "1_erros.csv"

COLS_SAIDA = ["conceito", "categoria", "termos", "codigos_catalogo", "origem"]
COLS_CATEGORIA = ["tipo", "codigo", "categoria"]
CAT_FALLBACK = "outros"

console = Console()


def _barra() -> Progress:
    return Progress(
        SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=30),
        MofNCompleteColumn(), TimeElapsedColumn(), console=console,
    )


def _norm(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (s or "").strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def carregar_catalogo() -> pd.DataFrame:
    if not CATALOGO_FILTRADO.exists():
        raise SystemExit(f"Catálogo filtrado ausente: {CATALOGO_FILTRADO}. Rode 0a antes.")
    return pd.read_csv(CATALOGO_FILTRADO, dtype=str, encoding="utf-8-sig").fillna("")


def gerar_por_item(df, criar_curador, concurrency):
    """Preenche checkpoints/1_termos_item.csv (tipo, codigo, termos, categoria) de forma resumível.

    Cada worker usa o SEU próprio Curador (cliente por thread): compartilhar um único cliente
    ChatOpenAI entre threads serializa as chamadas HTTP e mata a concorrência.
    """
    os.makedirs(CK_TERMOS.parent, exist_ok=True)
    feitas = ler_chaves_concluidas(str(CK_TERMOS), ("tipo", "codigo"))
    pendentes = [r for _, r in df.iterrows() if (r["tipo"], r["codigo"]) not in feitas]
    if not pendentes:
        console.print(f"[dim][1] Termos/categoria: todos os {len(feitas)} itens já feitos — pulando.[/]")
        return
    console.print(f"[bold][1] Gerando termos + categoria de {len(pendentes)} itens[/] "
                  f"(já feitos: {len(feitas)}, concorrência: {concurrency})")

    _tls = threading.local()
    def _curador():
        if not hasattr(_tls, "c"):
            _tls.c = criar_curador()
        return _tls.c

    n_erros = [0]
    with EscritorSeguro(str(CK_TERMOS), ["tipo", "codigo", "termos", "categoria"]) as w:
        def fn(row):
            cur = _curador()
            termos = cur.gerar_termos_item(row.get("nome_pdm", ""), row.get("descricao", ""),
                                           row.get("tipo", ""), row.get("nome_grupo", ""))
            cats = cur.classificar_categoria(row.get("descricao", ""))["categorias"]
            return {"termos": termos, "categoria": cats[0] if cats else ""}

        def ok(row, res):
            w.escrever({"tipo": row["tipo"], "codigo": row["codigo"],
                        "termos": "|".join(res["termos"]), "categoria": res["categoria"]})

        def err(row, exc):
            n_erros[0] += 1
            console.log(f"[red]erro[/] {row.get('tipo')}/{row.get('codigo')}: {exc}")
            erros_log.logar_erro(str(ERROS), "1", row.get("tipo"), row.get("codigo"),
                                 row.get("nome_pdm"), exc)

        with _barra() as progress:
            tarefa = progress.add_task("itens", total=len(pendentes))
            def prog(f, t):
                progress.update(tarefa, completed=f)
            executar_paralelo(pendentes, fn, concurrency=concurrency, on_result=ok,
                              on_error=err, on_progress=prog)
    if n_erros[0]:
        console.print(f"[yellow][1] {n_erros[0]} itens falharam — ver {ERROS}[/]")


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


def agregar_por_termo(df, checkpoint, categorias) -> list[dict]:
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
        console.print(f"[dim][1] {pulados} itens em '{CAT_FALLBACK}' (fora de escopo) pulados da busca.[/]")

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


def mesclar_preservando_manual(novas: list[dict]) -> list[dict]:
    """Mantém as linhas origem=manual; reconstrói as origem=llm; não duplica termo já manual."""
    manuais = [l for l in ler_csv(str(SAIDA)) if l.get("origem") == "manual"]
    termos_manual = {_norm(l.get("termos", "")) for l in manuais}
    novas = [l for l in novas if _norm(l["termos"]) not in termos_manual]
    if manuais:
        console.print(f"[dim][1] {len(manuais)} linhas origem=manual preservadas.[/]")
    return manuais + novas


def main():
    ap = argparse.ArgumentParser(description="Etapa 1 — termos genéricos por item")
    ap.add_argument("--provedor", choices=["local", "openrouter"], default="local")
    ap.add_argument("--limite", type=int, default=None, help="Processa só N itens do catálogo (teste)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--forte", action="store_true",
                    help="Usa o modelo PASS2 (mais caro/forte) em vez do PASS1. Só afeta 'openrouter'.")
    ap.add_argument("--regerar", action="store_true", help="Recria a saída do zero (confirmação)")
    args = ap.parse_args()

    cfg = carregar_config()
    msg = exigir(cfg, args.provedor)
    if msg:
        raise SystemExit(msg)

    if args.regerar and SAIDA.exists():
        resp = input(f"Apagar {SAIDA.name} e regerar do zero? (digite 'sim'): ")
        if resp.strip().lower() == "sim":
            SAIDA.unlink()
        else:
            raise SystemExit("Cancelado.")

    df = carregar_catalogo()
    if args.limite:
        df = df.head(args.limite)

    def criar_curador():
        return Curador.from_provedor(cfg, args.provedor, forte=args.forte, max_retries=6)

    modelo = "PASS2 (forte)" if args.forte else "PASS1"
    console.print(f"[dim][1] Provedor: {args.provedor} · modelo: {modelo}[/]")
    gerar_por_item(df, criar_curador, args.concurrency)

    checkpoint = _ler_checkpoint()
    categorias = resolver_categorias(df, checkpoint)
    escrever_csv(str(CATEGORIA_POR_CODIGO), COLS_CATEGORIA,
                 [{"tipo": r["tipo"], "codigo": r["codigo"], "categoria": categorias[r["codigo"]]}
                  for _, r in df.iterrows()])
    console.print(f"[bold green][1] Gravado[/] {CATEGORIA_POR_CODIGO} "
                  f"([bold]{len(df)}[/] itens, categoria nunca vazia).")

    novas = agregar_por_termo(df, checkpoint, categorias)
    final = mesclar_preservando_manual(novas)
    escrever_csv(str(SAIDA), COLS_SAIDA, final)
    console.print(f"[bold green][1] Gravado[/] {SAIDA} ([bold]{len(final)}[/] termos).")


if __name__ == "__main__":
    main()
