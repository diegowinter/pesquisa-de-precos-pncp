"""
Etapa 0a — Catálogos CATMAT (materiais) e CATSER (serviços) da API de Dados Abertos.

Baixa os catálogos do Compras.gov.br e aplica a allow-list curada. Mantida como etapa
separada porque o download é pesado e roda esporadicamente.

FASE 10 — `--fonte banco` (DEFAULT) não escreve NADA em disco (ADR-018):
    catalogo_raw     ← catálogo completo, uma página por transação
    catalogo_download← checkpoint de página (o que era a pasta de parquet-partes)
    catalogo_item    ← DERIVADO por SQL de `catalogo_raw ∩ pdm_permitido` (ADR-017)
    catalogo_snapshot← baseline do delta
A allow-list não vive mais no código: `pdm_permitido` é editável pela interface, e
`core/catalogo/local.py` guarda só o método de filtro. Mudar a curadoria e rederivar não
exige rebaixar a API.

RESUMÍVEL / à prova de queda nos dois caminhos: cada página é persistida assim que chega
(linha em `catalogo_download` ou parquet-parte em data/checkpoints/0a_parts_<tipo>/) e um
novo run pula o que já entrou. No pior caso perde-se a última página.

Entradas: nenhuma (é a raiz do grafo).
Saídas do caminho legado `--fonte csv` (nomes canônicos em config/paths.py):
    data/0a_catalogo_materiais.parquet
    data/0a_catalogo_servicos.parquet
    data/0a_catalogo_meta.json          (data do download e contagens)
    data/0a_catalogo_filtrado.csv       (allow-list curada aplicada)
    data/0a_catalogo_snapshot.csv       (baseline p/ o delta da próxima rodada)
    data/0a_catalogo_delta.csv          (tipo, codigo, status ∈ {novo, removido})
    data/checkpoints/0a_parts_<tipo>/   (temporário; removido ao final)
Chave de resumo: página da API (arquivo-parte por página).

NÃO fazer: apagar as partes antes do parquet final existir; tratar a primeira execução sem
snapshot como "tudo novo" (ver `gerar_delta_catalogo`).

Uso:
    python -m pesquisa_precos.etapas.e0a_catalogo                        # banco (default)
    python -m pesquisa_precos.etapas.e0a_catalogo --fonte csv            # caminho legado
    python -m pesquisa_precos.etapas.e0a_catalogo --so-grupos-seguranca  # só grupos de segurança
    python -m pesquisa_precos.etapas.e0a_catalogo --forcar               # re-baixa mesmo se existir
    python -m pesquisa_precos.etapas.e0a_catalogo --tipo material        # só materiais
"""

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

from typing import Literal

from pydantic import BaseModel, Field

from pesquisa_precos.config import paths
from pesquisa_precos.core.catalogo.local import GRUPOS_MATERIAIS, GRUPOS_SERVICOS, filtrar_curado
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa

CHAVE = "0a"
# 1.1.0 (Fase 10): ganhou `--fonte banco` — catalogo_raw + derivação da allow-list.
# A regra de curadoria não mudou: o banco reproduz `filtrar_curado()` código a código.
VERSAO_CODIGO = "1.1.0"

DATA_DIR = paths.DATA
# Checkpoints de páginas ficam em data/checkpoints/0a_parts_<tipo>/ (não são saída de etapa).
PARTS_DIR = paths.CHECKPOINTS

FONTES = {
    "material": {
        "base_url": "https://dadosabertos.compras.gov.br/modulo-material/4_consultarItemMaterial",
        "params_extra": {"bps": "false"},
        "grupos": GRUPOS_MATERIAIS,
        "parquet": paths.E0A_PARQUET_MATERIAIS,
    },
    "servico": {
        "base_url": "https://dadosabertos.compras.gov.br/modulo-servico/6_consultarItemServico",
        "params_extra": {},
        "grupos": GRUPOS_SERVICOS,
        "parquet": paths.E0A_PARQUET_SERVICOS,
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

SNAPSHOT = paths.E0A_SNAPSHOT
DELTA = paths.E0A_DELTA


class Params(BaseModel):
    tipo: str | None = Field(None, description="Baixar só um tipo: material ou servico")
    so_grupos_seguranca: bool = Field(
        False, description="Baixar só os grupos de segurança pública (rápido)")
    forcar: bool = Field(False, description="Re-baixar mesmo se o parquet já existir")
    fonte: Literal["banco", "csv"] = Field(
        "banco", description="Onde gravar o catálogo (banco = sem arquivo em disco)")

    def tipos(self) -> list[str]:
        return [self.tipo] if self.tipo else ["material", "servico"]


class _ErroPermanente(RuntimeError):
    """4xx que não adianta repetir (ex.: parâmetro realmente inválido)."""


def fetch_page(base_url: str, params: dict, ctx: ContextoExecucao) -> dict:
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
            ctx.log("aviso", f"Tentativa {attempt} falhou ({e}) — retry em {wait}s")
            time.sleep(wait)


def _gravar_parquet_atomico(df: pd.DataFrame, destino: Path):
    """Grava um parquet de forma atômica (tmp + replace), recriando a pasta se sumiu."""
    destino.parent.mkdir(parents=True, exist_ok=True)  # defensivo: pasta pode ter sido removida
    tmp = destino.with_suffix(destino.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, destino)


def coletar_para_partes(base_url: str, params_extra: dict, parts_dir: Path, prefixo: str,
                        ctx: ContextoExecucao, estado: dict) -> None:
    """
    Pagina os resultados gravando CADA página como um parquet-parte em `parts_dir`.
    Resumível: páginas já gravadas são puladas. Assim um download longo nunca é perdido —
    no pior caso perde-se só a última página, e um novo run continua de onde parou.
    """
    params = {"pagina": 1, "tamanhoPagina": TAM_PAGINA, **params_extra}
    first = fetch_page(base_url, params, ctx)
    total_paginas = first.get("totalPaginas", 1) or 1
    total_registros = first.get("totalRegistros", 0) or 0
    estado["total"] += total_registros
    ctx.progresso(estado["feitos"], estado["total"], descricao=estado["descricao"])

    for pagina in range(1, total_paginas + 1):
        parte = parts_dir / f"{prefixo}_p{pagina:05d}.parquet"
        if parte.exists():  # já baixada num run anterior → pula (resume)
            try:
                estado["feitos"] += len(pd.read_parquet(parte))
            except Exception:
                pass
            ctx.progresso(estado["feitos"], estado["total"], descricao=estado["descricao"])
            continue
        data = first if pagina == 1 else fetch_page(base_url, {**params, "pagina": pagina}, ctx)
        lote = data.get("resultado", [])
        if lote:
            _gravar_parquet_atomico(pd.DataFrame(lote), parte)
        estado["feitos"] += len(lote)
        ctx.progresso(estado["feitos"], estado["total"], descricao=estado["descricao"])
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


def baixar_tipo(tipo: str, so_grupos: bool, forcar: bool, ctx: ContextoExecucao,
                estado: dict) -> pd.DataFrame:
    fonte = FONTES[tipo]
    parts_dir = PARTS_DIR / f"0a_parts_{tipo}"
    if forcar and parts_dir.exists():
        shutil.rmtree(parts_dir, ignore_errors=True)
    parts_dir.mkdir(parents=True, exist_ok=True)

    estado["descricao"] = f"[cyan]{tipo}[/]"
    if so_grupos:
        for codigo in sorted(fonte["grupos"]):
            coletar_para_partes(
                fonte["base_url"], {**fonte["params_extra"], "codigoGrupo": codigo},
                parts_dir, f"g{codigo}", ctx, estado,
            )
    else:
        coletar_para_partes(fonte["base_url"], fonte["params_extra"], parts_dir, "full",
                            ctx, estado)

    return _consolidar_partes(parts_dir, tipo)


def gerar_catalogo_filtrado(tipos: list[str], ctx: ContextoExecucao) -> tuple[int, dict]:
    """
    Gera data/0a_catalogo_filtrado.csv aplicando a allow-list curada (PDM p/ materiais,
    codigoServico p/ serviços) sobre os parquet baixados. Substitui a curadoria por LLM.
    """
    saida = paths.E0A_CATALOGO
    partes = []
    por_tipo: dict[str, int] = {}
    for tipo in tipos:
        parquet = FONTES[tipo]["parquet"]
        if not parquet.exists():
            ctx.log("aviso", f"{tipo}: parquet ausente — pulando no filtrado.")
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
        ctx.log("info", f"[green]{tipo}: {len(sub):,} itens no filtrado[/]")
        por_tipo[tipo] = len(sub)
        partes.append(sub)

    if not partes:
        ctx.log("aviso", "Nada a gravar no catálogo filtrado.")
        return 0, {}
    combinado = pd.concat(partes, ignore_index=True)
    combinado.to_csv(saida, index=False, encoding="utf-8-sig")
    ctx.log("info", f"[bold green]Catálogo filtrado: {len(combinado):,} itens[/] → {saida}")
    delta = gerar_delta_catalogo(combinado, ctx)
    return len(combinado), {**por_tipo, **delta}


def gerar_delta_catalogo(combinado: pd.DataFrame, ctx: ContextoExecucao) -> dict:
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
    ctx.log("info", f"[bold]Delta do catálogo:[/] {len(novos)} novos, "
                    f"{len(removidos)} removidos → {DELTA}")
    return {"codigos_novos": len(novos), "codigos_removidos": len(removidos)}


# ── Caminho `--fonte banco` (Fase 10) ───────────────────────────────────────────────
#
# Mesma paginação e mesmo resume do caminho em disco; muda ONDE cada página cai. Em vez de
# um parquet-parte por página + consolidação no fim, cada página é gravada direto em
# `catalogo_raw` (upsert) e a página é marcada em `catalogo_download`. Não há consolidação:
# a deduplicação por `(tipo, codigo)` é a PK da tabela, não um `drop_duplicates` no fim.

def _texto(valor: object) -> str | None:
    """Campo da API → text do Postgres. Número vira string (o catálogo mistura int e str no
    mesmo campo entre endpoints); vazio vira NULL, nunca string vazia — `''` e `NULL` casariam
    diferente no join da derivação."""
    if valor is None:
        return None
    s = str(valor).strip()
    return s or None


def _linha_raw(tipo: str, reg: dict) -> tuple | None:
    """Registro da API → tupla na ordem de `curadoria.COLUNAS_RAW`.

    O mapeamento é o MESMO de `gerar_catalogo_filtrado()` — material e serviço têm nomes de
    campo diferentes na origem, e é aqui que eles viram um formato só. Manter os dois lugares
    em sincronia é o preço de ter dois caminhos; quando `--fonte csv` sair, sobra este.
    """
    if tipo == "material":
        codigo = _texto(reg.get("codigoItem"))
        if not codigo:
            return None
        return ("material", codigo, _texto(reg.get("codigoPdm")), _texto(reg.get("nomePdm")),
                _texto(reg.get("descricaoItem")) or "", _texto(reg.get("codigoGrupo")),
                _texto(reg.get("nomeGrupo")), _texto(reg.get("nomeClasse")))
    codigo = _texto(reg.get("codigoServico"))
    if not codigo:
        return None
    return ("servico", codigo, None, None,
            _texto(reg.get("nomeServico")) or "", _texto(reg.get("codigoGrupo")),
            _texto(reg.get("nomeGrupo")), _texto(reg.get("nomeClasse")))


def coletar_para_banco(tipo: str, base_url: str, params_extra: dict, prefixo: str,
                       ctx: ContextoExecucao, estado: dict) -> int:
    """Pagina gravando cada página em `catalogo_raw`. Devolve quantas linhas entraram."""
    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import curadoria as repo

    params = {"pagina": 1, "tamanhoPagina": TAM_PAGINA, **params_extra}
    first = fetch_page(base_url, params, ctx)
    total_paginas = first.get("totalPaginas", 1) or 1
    estado["total"] += first.get("totalRegistros", 0) or 0
    ctx.progresso(estado["feitos"], estado["total"], descricao=estado["descricao"])

    with db.sessao() as s:
        ja_feitas = repo.paginas_baixadas(s, tipo, prefixo)

    gravadas = 0
    for pagina in range(1, total_paginas + 1):
        if ctx.cancelado():
            break
        if pagina in ja_feitas:
            ctx.progresso(estado["feitos"], estado["total"], descricao=estado["descricao"])
            continue
        data = first if pagina == 1 else fetch_page(base_url, {**params, "pagina": pagina}, ctx)
        linhas = [ln for ln in (_linha_raw(tipo, r) for r in data.get("resultado", []))
                  if ln is not None]
        # Uma transação por página: a gravação das linhas e a marca da página vivem ou morrem
        # juntas. `conexao_bruta` (COPY) e `sessao` (ORM) são conexões distintas, então o
        # commit do COPY vem primeiro — se o processo cair entre os dois, a página é
        # rebaixada e o upsert absorve a repetição.
        if linhas:
            with db.conexao_bruta() as conn:
                repo.gravar_raw(conn, linhas)
        with db.sessao() as s:
            repo.marcar_pagina(s, tipo, prefixo, pagina, len(linhas))
            s.commit()
        gravadas += len(linhas)
        estado["feitos"] += len(linhas)
        ctx.progresso(estado["feitos"], estado["total"], descricao=estado["descricao"])
        if pagina < total_paginas:
            time.sleep(0.3)
    return gravadas


def baixar_tipo_para_banco(tipo: str, so_grupos: bool, forcar: bool,
                           ctx: ContextoExecucao, estado: dict) -> int:
    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import curadoria as repo

    fonte = FONTES[tipo]
    if forcar:
        with db.sessao() as s:
            repo.limpar_download(s, tipo)
            s.commit()

    estado["descricao"] = f"[cyan]{tipo}[/]"
    if so_grupos:
        # Os grupos vêm de `grupo_permitido` (ADR-017), não mais das constantes do módulo.
        # Lista vazia é erro explícito: baixar o catálogo inteiro "para compensar" seria
        # ignorar em silêncio a flag que o operador digitou justamente para baixar menos.
        with db.sessao() as s:
            grupos = repo.grupos_ativos(s, tipo)
        if not grupos:
            raise SystemExit(
                f"Nenhum grupo ativo para {tipo} em grupo_permitido — cadastre pela interface "
                f"ou rode sem --so-grupos-seguranca para baixar o catálogo inteiro.")
        ctx.log("debug", f"[dim][0a] {tipo}: {len(grupos)} grupos de segurança "
                         f"({', '.join(grupos)})[/]")
        total = 0
        for codigo in grupos:
            total += coletar_para_banco(
                tipo, fonte["base_url"], {**fonte["params_extra"], "codigoGrupo": codigo},
                f"g{codigo}", ctx, estado)
        return total
    return coletar_para_banco(tipo, fonte["base_url"], fonte["params_extra"], "full",
                              ctx, estado)


def executar_no_banco(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    """0a inteira sem tocar em disco (ADR-018): baixa → `catalogo_raw` → deriva
    `catalogo_item` pela allow-list → delta por snapshot no banco."""
    from sqlalchemy import text as text_sql

    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import curadoria as repo

    ok, detalhe = db.esta_disponivel()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env "
                         f"ou rode com --fonte csv.")

    tipos = params.tipos()
    ctx.log("info", f"[bold]Download do catálogo (CATMAT/CATSER) → banco[/] · grupos: "
                    f"[cyan]{'só segurança pública' if params.so_grupos_seguranca else 'todos'}[/]")

    estado = {"feitos": 0, "total": 0, "descricao": "catálogo"}
    por_tipo: dict[str, int] = {}
    for tipo in tipos:
        if ctx.cancelado():
            break
        n = baixar_tipo_para_banco(tipo, params.so_grupos_seguranca, params.forcar, ctx, estado)
        por_tipo[f"{tipo}_baixados"] = n
        ctx.log("info", f"[green]{tipo}: {n:,} linhas em catalogo_raw[/]")

    with db.sessao() as s:
        total_raw = repo.contar_raw(s)
        derivacao = repo.derivar_catalogo_item(s)
        delta = repo.delta_catalogo(s)
        s.commit()
        preview = [
            {"tipo": t, "codigo": c, "descricao": (d or "")[:80]}
            for t, c, d in s.execute(text_sql(
                "SELECT tipo::text, codigo, descricao FROM catalogo_item "
                "WHERE ativo ORDER BY tipo, codigo LIMIT 20")).all()
        ]

    ctx.log("info", f"[bold]Catálogo completo:[/] {total_raw:,} · "
                    f"[bold green]curado: {derivacao['ativos']:,}[/] "
                    f"({derivacao['desativados']} desativados)")
    if delta.get("baseline"):
        ctx.log("info", "[dim]Primeiro snapshot no banco — delta zerado por definição.[/]")
    else:
        ctx.log("info", f"[bold]Delta:[/] {delta['codigos_novos']} novos, "
                        f"{delta['codigos_removidos']} removidos")

    return ResultadoEtapa(
        processados=derivacao["ativos"], erros=0,
        metricas={"itens_no_catalogo_raw": total_raw, **por_tipo, **derivacao, **delta},
        preview=preview,
    )


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Quantos catálogos serão baixados. Sem LLM: o custo é tempo de HTTP, não dinheiro."""
    if params.fonte == "banco":
        from pesquisa_precos.db import sessao as db
        from pesquisa_precos.db.repos import curadoria as repo

        detalhes = {"fonte": "banco",
                    "grupos": "só segurança pública" if params.so_grupos_seguranca else "todos"}
        ok, detalhe = db.esta_disponivel()
        if not ok:
            return Estimativa(detalhes={**detalhes, "aviso": f"banco indisponível: {detalhe}"})
        with db.sessao() as s:
            for tipo in params.tipos():
                # A unidade útil aqui é "quanto já está no banco": o resume é por página, então
                # o que falta baixar só se sabe consultando a API — que é justamente o que uma
                # estimativa não pode fazer (ela não gasta nada, nem tempo de rede).
                detalhes[tipo] = f"{repo.contar_raw(s, tipo):,} linhas já em catalogo_raw"
            detalhes["curados_hoje"] = len(repo.listar_permitidos(s))
        return Estimativa(unidades=len(params.tipos()), chamadas_llm=0, detalhes=detalhes)

    a_baixar = [t for t in params.tipos()
                if params.forcar or not FONTES[t]["parquet"].exists()]
    detalhes = {t: ("re-baixa" if params.forcar else
                    ("já existe — pula" if FONTES[t]["parquet"].exists() else "baixa"))
                for t in params.tipos()}
    detalhes["grupos"] = "só segurança pública" if params.so_grupos_seguranca else "todos"
    return Estimativa(unidades=len(a_baixar), chamadas_llm=0, detalhes=detalhes)


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    if params.fonte == "banco":
        return executar_no_banco(params, ctx)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tipos = params.tipos()

    ctx.log("info", f"[bold]Download do catálogo (CATMAT/CATSER)[/] · grupos: "
                    f"[cyan]{'só segurança pública' if params.so_grupos_seguranca else 'todos'}[/]"
                    f" · destino: [green]{DATA_DIR}[/]")

    meta = {}
    if paths.E0A_META.exists():
        meta = json.loads(paths.E0A_META.read_text(encoding="utf-8"))

    estado = {"feitos": 0, "total": 0, "descricao": "catálogo"}
    baixados = 0
    for tipo in tipos:
        if ctx.cancelado():
            break
        parquet = FONTES[tipo]["parquet"]
        if parquet.exists() and not params.forcar:
            ctx.log("debug", f"[dim]{tipo}: {parquet.name} já existe — pulando "
                             f"(use --forcar para rebaixar).[/]")
            continue
        df = baixar_tipo(tipo, params.so_grupos_seguranca, params.forcar, ctx, estado)
        _gravar_parquet_atomico(df, parquet)  # atômico + recria a pasta se necessário
        # Só remove as partes DEPOIS do parquet final existir (evita perder o download).
        shutil.rmtree(PARTS_DIR / f"0a_parts_{tipo}", ignore_errors=True)
        meta[tipo] = {
            "linhas": int(len(df)),
            "so_grupos_seguranca": bool(params.so_grupos_seguranca),
            "baixado_em": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        baixados += 1
        ctx.log("info", f"[green]{tipo}: {len(df):,} itens[/] → {parquet}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)  # defensivo
    paths.E0A_META.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # Aplica a allow-list curada e grava o catálogo filtrado (materiais + serviços).
    n_filtrado, metricas = gerar_catalogo_filtrado(tipos, ctx)

    preview = []
    if paths.E0A_CATALOGO.exists():
        amostra = pd.read_csv(paths.E0A_CATALOGO, dtype=str, encoding="utf-8-sig",
                              nrows=20).fillna("")
        preview = amostra[["tipo", "codigo", "descricao"]].to_dict("records")
    return ResultadoEtapa(
        processados=n_filtrado, erros=0,
        metricas={"tipos_baixados": baixados, "itens_no_filtrado": n_filtrado, **metricas},
        preview=preview,
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
