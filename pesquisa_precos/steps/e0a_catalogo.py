"""
Etapa 0a — Catálogos CATMAT (materiais) e CATSER (serviços) da API de Dados Abertos.

Baixa os catálogos do Compras.gov.br e aplica a allow-list curada. Mantida como etapa
separada porque o download é pesado e roda esporadicamente.

A etapa não escreve NADA em disco (ADR-018/ADR-020):
    catalogo_raw     ← catálogo completo, uma página por transação
    catalogo_download← checkpoint de página (o que era a pasta de parquet-partes)

Baixar é tudo o que ela faz. O CORTE (`catalogo_raw ∩ pdm_permitido` → `catalogo_item`)
mudou-se para a etapa 0b em 2026-08-23: é decisão do operador sobre o escopo do pipeline
inteiro, e merece o próprio gate em vez de acontecer de brinde no fim do download.

RESUMÍVEL / à prova de queda: cada página é persistida assim que chega (linha em
`catalogo_download`) e um novo run pula o que já entrou. No pior caso perde-se a última.

Entradas: nenhuma (é a raiz do grafo).
Chave de resumo: página da API (linha em `catalogo_download`).

"""

import sys
import time

import requests

from pesquisa_precos.core.collection import http

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from pydantic import BaseModel, Field

from pesquisa_precos.core.catalogo.local import GRUPOS_MATERIAIS, GRUPOS_SERVICOS
from pesquisa_precos.steps.base import RunContext, Estimate, StepResult

KEY = "0a"
# 3.0.0: o corte saiu daqui e virou a etapa 0b — esta só baixa.
CODE_VERSION = "3.0.0"

FONTES = {
    "material": {
        "base_url": "https://dadosabertos.compras.gov.br/modulo-material/4_consultarItemMaterial",
        "params_extra": {"bps": "false"},
        "grupos": GRUPOS_MATERIAIS,
    },
    "servico": {
        "base_url": "https://dadosabertos.compras.gov.br/modulo-servico/6_consultarItemServico",
        "params_extra": {},
        "grupos": GRUPOS_SERVICOS,
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

class Params(BaseModel):
    tipo: str | None = Field(None, description="Baixar só um tipo: material ou servico")
    so_grupos_seguranca: bool = Field(
        False, description="Baixar só os grupos de segurança pública (rápido)")
    forcar: bool = Field(False, description="Re-baixar mesmo o que já está em catalogo_raw")

    def tipos(self) -> list[str]:
        return [self.tipo] if self.tipo else ["material", "servico"]


class _ErroPermanente(RuntimeError):
    """4xx que não adianta repetir (ex.: parâmetro realmente inválido)."""


def fetch_page(base_url: str, params: dict, ctx: RunContext) -> dict:
    """Uma página do catálogo, com retry/backoff (padrão dos clientes do repo)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = http.get(base_url, params=params, headers=HEADERS)
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


# ── Gravação no banco (Fase 10) ─────────────────────────────────────────────────────
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
    é a única — o caminho de parquet/CSV saiu na Fase 13.
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
                       ctx: RunContext, estado: dict) -> int:
    """Pagina gravando cada página em `catalogo_raw`. Devolve quantas linhas entraram."""
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import curation as repo

    params = {"pagina": 1, "tamanhoPagina": TAM_PAGINA, **params_extra}
    first = fetch_page(base_url, params, ctx)
    total_paginas = first.get("totalPaginas", 1) or 1
    estado["total"] += first.get("totalRegistros", 0) or 0
    ctx.progresso(estado["feitos"], estado["total"], descricao=estado["descricao"])

    with db.session() as s:
        ja_feitas = repo.paginas_baixadas(s, tipo, prefixo)
        # Retomar não é recomeçar: o que já está no banco entra no progresso desde o início,
        # senão a barra volta a zero e parece que a etapa perdeu o trabalho anterior.
        estado["feitos"] += repo.linhas_ja_baixadas(s, tipo, prefixo)

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
        # juntas. `raw_connection` (COPY) e `sessao` (ORM) são conexões distintas, então o
        # commit do COPY vem primeiro — se o processo cair entre os dois, a página é
        # rebaixada e o upsert absorve a repetição.
        if linhas:
            with db.raw_connection() as conn:
                repo.gravar_raw(conn, linhas)
        with db.session() as s:
            repo.marcar_pagina(s, tipo, prefixo, pagina, len(linhas))
            s.commit()
        gravadas += len(linhas)
        estado["feitos"] += len(linhas)
        ctx.progresso(estado["feitos"], estado["total"], descricao=estado["descricao"])
        if pagina < total_paginas:
            time.sleep(0.3)
    return gravadas


def baixar_tipo_para_banco(tipo: str, so_grupos: bool, forcar: bool,
                           ctx: RunContext, estado: dict) -> int:
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import curation as repo

    fonte = FONTES[tipo]
    if forcar:
        with db.session() as s:
            repo.limpar_download(s, tipo)
            s.commit()

    estado["descricao"] = f"[cyan]{tipo}[/]"
    if so_grupos:
        # Os grupos vêm de `grupo_permitido` (ADR-017), não mais das constantes do módulo.
        # Lista vazia é erro explícito: baixar o catálogo inteiro "para compensar" seria
        # ignorar em silêncio a flag que o operador digitou justamente para baixar menos.
        with db.session() as s:
            grupos = repo.grupos_ativos(s, tipo)
        if not grupos:
            raise SystemExit(
                f"Nenhum grupo active para {tipo} em grupo_permitido — cadastre pela interface "
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


def run(params: Params, ctx: RunContext) -> StepResult:
    """Baixa o catálogo para `catalogo_raw` e para por aí. Quem aplica a allow-list é a 0b."""
    from sqlalchemy import text as text_sql

    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import curation as repo

    ok, detalhe = db.is_available()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")

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

    with db.session() as s:
        total_raw = repo.contar_raw(s)
        preview = [
            {"tipo": t, "codigo": c, "descricao": (d or "")[:80]}
            for t, c, d in s.execute(text_sql(
                "SELECT tipo::text, codigo, description FROM catalogo_raw "
                "ORDER BY tipo, codigo LIMIT 20")).all()
        ]

    ctx.log("info", f"[bold]Catálogo completo no banco:[/] {total_raw:,} linhas · "
                    "[dim]o corte é a etapa 0b[/]")

    return StepResult(
        processed=estado["feitos"], errors=0,
        resumo=f"{total_raw:,} linhas no catálogo completo (o corte é a etapa 0b)",
        metrics={"itens_no_catalogo_raw": total_raw, **por_tipo},
        preview=preview,
    )


def estimate(params: Params, ctx: RunContext) -> Estimate:
    """Quantos catálogos serão baixados. Sem LLM: o custo é tempo de HTTP, não dinheiro."""
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import curation as repo

    detalhes = {"grupos": "só segurança pública" if params.so_grupos_seguranca else "todos"}
    ok, detalhe = db.is_available()
    if not ok:
        return Estimate(detalhes={**detalhes, "aviso": f"banco indisponível: {detalhe}"})
    with db.session() as s:
        for tipo in params.tipos():
            # A unidade útil aqui é "quanto já está no banco": o resume é por página, então
            # o que falta baixar só se sabe consultando a API — que é justamente o que uma
            # estimativa não pode fazer (ela não gasta nada, nem tempo de rede).
            detalhes[tipo] = f"{repo.contar_raw(s, tipo):,} linhas já em catalogo_raw"
    return Estimate(unidades=len(params.tipos()), chamadas_llm=0, detalhes=detalhes)
