"""
Baixa os catálogos CATMAT (materiais) e CATSER (serviços) da API de Dados Abertos
do Compras.gov.br e grava em Parquet (via Pandas), para recarga rápida na curadoria.

Este é o passo de "montar catálogo" (equivalente ao passo 1 dos pipelines itens-cat-v*),
mantido como script separado porque o download é pesado e roda esporadicamente.

RESUMÍVEL / à prova de queda: cada página é gravada em disco assim que chega, como um
parquet-parte em data/catalogo/_parts_<tipo>/. Se o processo cair (ou a pasta sumir),
basta rodar de novo — ele pula as páginas já baixadas e continua de onde parou. As partes
só são apagadas DEPOIS que o parquet final é gravado com sucesso.

Saída:
    data/catalogo/materiais.parquet
    data/catalogo/servicos.parquet
    data/catalogo/_meta.json        (data do download e contagens)
    data/catalogo/_parts_<tipo>/    (temporário; removido ao final)

Uso:
    python obter_catalogo.py                     # baixa tudo (só se faltando/antigo)
    python obter_catalogo.py --so-grupos-seguranca  # baixa só os grupos de segurança (rápido, p/ POC)
    python obter_catalogo.py --forcar            # re-baixa mesmo se já existir
    python obter_catalogo.py --tipo material     # baixa só materiais (ou 'servico')
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pandas as pd
import requests

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from scripts.catalogo_local import GRUPOS_MATERIAIS, GRUPOS_SERVICOS, filtrar_curado

console = Console()

DATA_DIR = Path(__file__).resolve().parent / "data"
# Checkpoints de páginas ficam em data/checkpoints/0a_parts_<tipo>/ (não são saída de etapa).
PARTS_DIR = DATA_DIR / "checkpoints"

FONTES = {
    "material": {
        "base_url": "https://dadosabertos.compras.gov.br/modulo-material/4_consultarItemMaterial",
        "params_extra": {"bps": "false"},
        "grupos": GRUPOS_MATERIAIS,
        "parquet": DATA_DIR / "0a_catalogo_materiais.parquet",
    },
    "servico": {
        "base_url": "https://dadosabertos.compras.gov.br/modulo-servico/6_consultarItemServico",
        "params_extra": {},
        "grupos": GRUPOS_SERVICOS,
        "parquet": DATA_DIR / "0a_catalogo_servicos.parquet",
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

TAM_PAGINA = 500

# A API às vezes devolve HTTP 400 para erros TRANSITÓRIOS do servidor (hiccup de banco),
# não para parâmetros inválidos. Nesses casos o corpo traz uma destas marcas → vale retry.
MARCAS_400_TRANSITORIO = (
    "could not open jpa",
    "entitymanager",
    "erro ao efetuar a consulta",
    "transaction",
    "timeout",
    "connection",
    "deadlock",
)


class _ErroPermanente(RuntimeError):
    """4xx que não adianta repetir (ex.: parâmetro realmente inválido)."""


def fetch_page(base_url: str, params: dict) -> dict:
    """Uma página do catálogo, com retry/backoff (padrão dos clientes do repo)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.get(base_url, params=params, headers=HEADERS, timeout=(30, 300))
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                corpo = (resp.text or "").lower()
                if any(m in corpo for m in MARCAS_400_TRANSITORIO):
                    # 400 transitório do servidor → cai no retry com backoff (não é permanente).
                    raise requests.exceptions.HTTPError(
                        f"HTTP {resp.status_code} transitório: {resp.text[:120]}"
                    )
                # 4xx de fato permanente (parâmetro inválido) → não repetir para sempre.
                raise _ErroPermanente(
                    f"HTTP {resp.status_code} (parâmetros inválidos?) em {resp.url} — {resp.text[:200]}"
                )
            resp.raise_for_status()
            return resp.json()
        except _ErroPermanente:
            raise
        except Exception as e:
            wait = min(attempt * 5, 60)
            console.log(f"[yellow]Tentativa {attempt} falhou[/] ({e}) — retry em {wait}s")
            time.sleep(wait)


def _gravar_parquet_atomico(df: pd.DataFrame, destino: Path):
    """Grava um parquet de forma atômica (tmp + replace), recriando a pasta se sumiu."""
    destino.parent.mkdir(parents=True, exist_ok=True)  # defensivo: pasta pode ter sido removida
    tmp = destino.with_suffix(destino.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, destino)


def coletar_para_partes(base_url: str, params_extra: dict, parts_dir: Path, prefixo: str, progress: Progress, task) -> None:
    """
    Pagina os resultados gravando CADA página como um parquet-parte em `parts_dir`.
    Resumível: páginas já gravadas são puladas. Assim um download longo nunca é perdido —
    no pior caso perde-se só a última página, e um novo run continua de onde parou.
    """
    params = {"pagina": 1, "tamanhoPagina": TAM_PAGINA, **params_extra}
    first = fetch_page(base_url, params)
    total_paginas = first.get("totalPaginas", 1) or 1
    total_registros = first.get("totalRegistros", 0) or 0
    progress.update(task, total=(progress.tasks[task].total or 0) + total_registros)

    for pagina in range(1, total_paginas + 1):
        parte = parts_dir / f"{prefixo}_p{pagina:05d}.parquet"
        if parte.exists():  # já baixada num run anterior → pula (resume)
            try:
                progress.update(task, advance=len(pd.read_parquet(parte)))
            except Exception:
                pass
            continue
        data = first if pagina == 1 else fetch_page(base_url, {**params, "pagina": pagina})
        lote = data.get("resultado", [])
        if lote:
            _gravar_parquet_atomico(pd.DataFrame(lote), parte)
        progress.update(task, advance=len(lote))
        if pagina < total_paginas:
            time.sleep(0.3)


def _consolidar_partes(parts_dir: Path, tipo: str) -> pd.DataFrame:
    """Junta todos os parquet-partes num DataFrame, deduplicando pela chave do catálogo."""
    partes = sorted(parts_dir.glob("*.parquet"))
    if not partes:
        return pd.DataFrame()
    df = pd.concat((pd.read_parquet(p) for p in partes), ignore_index=True)
    chave = "codigoItem" if tipo == "material" else "codigoServico"
    if chave in df.columns:
        df = df.drop_duplicates(subset=[chave]).reset_index(drop=True)
    return df


def baixar_tipo(tipo: str, so_grupos: bool, forcar: bool, progress: Progress) -> pd.DataFrame:
    fonte = FONTES[tipo]
    parts_dir = PARTS_DIR / f"0a_parts_{tipo}"
    if forcar and parts_dir.exists():
        shutil.rmtree(parts_dir, ignore_errors=True)
    parts_dir.mkdir(parents=True, exist_ok=True)

    task = progress.add_task(f"[cyan]{tipo}[/]", total=0)
    if so_grupos:
        for codigo in sorted(fonte["grupos"]):
            coletar_para_partes(
                fonte["base_url"], {**fonte["params_extra"], "codigoGrupo": codigo},
                parts_dir, f"g{codigo}", progress, task,
            )
    else:
        coletar_para_partes(fonte["base_url"], fonte["params_extra"], parts_dir, "full", progress, task)

    return _consolidar_partes(parts_dir, tipo)


def gerar_catalogo_filtrado(tipos: list[str]) -> None:
    """
    Gera data/0a_catalogo_filtrado.csv aplicando a allow-list curada (PDM p/ materiais,
    codigoServico p/ serviços) sobre os parquet baixados. Substitui a curadoria por LLM.
    """
    saida = DATA_DIR / "0a_catalogo_filtrado.csv"
    partes = []
    for tipo in tipos:
        parquet = FONTES[tipo]["parquet"]
        if not parquet.exists():
            console.print(f"[yellow]{tipo}: parquet ausente — pulando no filtrado.[/]")
            continue
        df = filtrar_curado(tipo, pd.read_parquet(parquet))
        if tipo == "material":
            sub = pd.DataFrame({
                "tipo": "material",
                "codigo": df["codigoItem"],
                "codigo_pdm": df["codigoPdm"],
                "nome_pdm": df["nomePdm"],
                "descricao": df["descricaoItem"],
                "codigo_grupo": df["codigoGrupo"],
                "nome_grupo": df["nomeGrupo"],
                "nome_classe": df["nomeClasse"],
            })
        else:
            sub = pd.DataFrame({
                "tipo": "servico",
                "codigo": df["codigoServico"],
                "codigo_pdm": pd.NA,
                "nome_pdm": pd.NA,
                "descricao": df["nomeServico"],
                "codigo_grupo": df["codigoGrupo"],
                "nome_grupo": df["nomeGrupo"],
                "nome_classe": df["nomeClasse"],
            })
        console.print(f"[green]{tipo}: {len(sub):,} itens no filtrado[/]")
        partes.append(sub)

    if not partes:
        console.print("[yellow]Nada a gravar no catálogo filtrado.[/]")
        return
    combinado = pd.concat(partes, ignore_index=True)
    combinado.to_csv(saida, index=False, encoding="utf-8-sig")
    console.print(f"[bold green]Catálogo filtrado: {len(combinado):,} itens[/] → {saida}")
    gerar_delta_catalogo(combinado)


SNAPSHOT = DATA_DIR / "0a_catalogo_snapshot.csv"
DELTA = DATA_DIR / "0a_catalogo_delta.csv"


def gerar_delta_catalogo(combinado: pd.DataFrame) -> None:
    """
    Compara o catálogo filtrado recém-gerado com o snapshot da rodada anterior e grava
    data/0a_catalogo_delta.csv (tipo, codigo, status ∈ {novo, removido}); ao final atualiza
    o snapshot para o estado atual.

    Primeira execução (sem snapshot): apenas estabelece a linha de base — delta vazio, para
    NÃO marcar como 'novo' um catálogo que já foi coletado (caso do seed do v3). O delta é
    consumido pela poda de 'removido' na etapa 8 e por relatório; a geração de termos (etapa 1)
    já é resumível por (tipo, codigo), então captura os 'novo' por conta própria.
    """
    atual = combinado[["tipo", "codigo"]].dropna().astype(str).drop_duplicates()
    chave_atual = set(map(tuple, atual.values))
    if SNAPSHOT.exists():
        snap = pd.read_csv(SNAPSHOT, dtype=str, encoding="utf-8-sig").fillna("")
        chave_prev = set(map(tuple, snap[["tipo", "codigo"]].astype(str).values))
    else:
        chave_prev = chave_atual  # baseline: nada é 'novo' na primeira vez
    novos = chave_atual - chave_prev
    removidos = chave_prev - chave_atual
    linhas = [{"tipo": t, "codigo": c, "status": "novo"} for (t, c) in sorted(novos)]
    linhas += [{"tipo": t, "codigo": c, "status": "removido"} for (t, c) in sorted(removidos)]
    pd.DataFrame(linhas, columns=["tipo", "codigo", "status"]).to_csv(
        DELTA, index=False, encoding="utf-8-sig")
    atual.to_csv(SNAPSHOT, index=False, encoding="utf-8-sig")
    console.print(f"[bold]Delta do catálogo:[/] {len(novos)} novos, "
                  f"{len(removidos)} removidos → {DELTA}")


def main():
    parser = argparse.ArgumentParser(description="Baixar catálogos CATMAT/CATSER para Parquet")
    parser.add_argument("--tipo", choices=["material", "servico"], help="Baixar só um tipo (default: ambos)")
    parser.add_argument("--so-grupos-seguranca", action="store_true", help="Baixar só os grupos de segurança pública (rápido)")
    parser.add_argument("--forcar", action="store_true", help="Re-baixar mesmo se o parquet já existir")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tipos = [args.tipo] if args.tipo else ["material", "servico"]

    console.print(Panel(
        f"Grupos: [cyan]{'só segurança pública' if args.so_grupos_seguranca else 'todos'}[/]\n"
        f"Destino: [green]{DATA_DIR}[/]",
        title="[bold]Download do catálogo (CATMAT/CATSER)[/]", border_style="blue", expand=False,
    ))

    meta = {}
    if (DATA_DIR / "0a_catalogo_meta.json").exists():
        meta = json.loads((DATA_DIR / "0a_catalogo_meta.json").read_text(encoding="utf-8"))

    progress = Progress(
        SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=30),
        MofNCompleteColumn(), TimeElapsedColumn(), console=console,
    )
    with progress:
        for tipo in tipos:
            parquet = FONTES[tipo]["parquet"]
            if parquet.exists() and not args.forcar:
                console.print(f"[dim]{tipo}: {parquet.name} já existe — pulando (use --forcar para rebaixar).[/]")
                continue
            df = baixar_tipo(tipo, args.so_grupos_seguranca, args.forcar, progress)
            _gravar_parquet_atomico(df, parquet)  # atômico + recria a pasta se necessário
            # Só remove as partes DEPOIS do parquet final existir (evita perder o download).
            shutil.rmtree(PARTS_DIR / f"0a_parts_{tipo}", ignore_errors=True)
            meta[tipo] = {
                "linhas": int(len(df)),
                "so_grupos_seguranca": bool(args.so_grupos_seguranca),
                "baixado_em": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            console.print(f"[green]{tipo}: {len(df):,} itens[/] → {parquet}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)  # defensivo
    (DATA_DIR / "0a_catalogo_meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # Aplica a allow-list curada e grava o catálogo filtrado (materiais + serviços).
    gerar_catalogo_filtrado(tipos)


if __name__ == "__main__":
    main()
