"""
Etapa 6a — Gera os pares candidatos (catálogo x item, dentro da mesma categoria) e descarta os
implausíveis.

O produto é restrito à mesma categoria, e um item multi-label pareia em todas as suas. Não há
dedup de pares: é regra de negócio. Cada par recebe um score léxico (BM25 normalizado por
categoria) e um semântico (cosseno), e sobrevive se `max(bm25, cosseno) >= piso` — basta um
dos dois sinais dizer "pode ser".

O trabalho pesado roda no serviço de `matching` (ADR-021); esta etapa lê o catálogo e os itens
do banco, manda, e grava os pares que voltam.

O corte top-K + piso é aplicado durante a geração dos pares, nunca depois de materializar o
produto cartesiano: um corte pós-hoc com `groupby().rank()` sobre o DataFrame inteiro já
estourou a memória com ~33 milhões de linhas.
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from sqlalchemy import text as sa_text
from pydantic import BaseModel, Field

from pesquisa_precos.steps.base import RunContext, Estimate, StepResult

KEY = "6a"
CODE_VERSION = "2.0.0"


class Params(BaseModel):
    sem_embedding: bool = Field(False, description="Só BM25 (pula o embedder/GPU)")
    top_k: int = Field(
        100, ge=1, description="Por código, mantém só os K itens PNCP mais similares")
    piso: float = Field(
        0.40, ge=0.0, le=1.0,
        description="Score efetivo mínimo (max bm25/cosseno) p/ o par sobreviver")


# ── Pareamento no banco + capacidade `pareamento` (Fases 10 e 11) ───────────────────
#
# Duas mudanças na mesma passada, porque tocam o mesmo corpo:
#   Fase 10 — catálogo e itens vêm do banco; os pares vão para a tabela `par`;
#   Fase 11 — BM25 + cosseno + corte saem do processo e viram a capacidade `pareamento`.
#
# O corte em streaming NÃO ficou para trás na mudança: ele agora vive em
# `core/pareamento/motor.py`, que roda tanto no serviço quanto em processo. O aviso do
# `MemoryError` de ~33M linhas vale igual dos dois lados.

def _exigir_banco():
    from pesquisa_precos.db import session as db

    ok, detalhe = db.is_available()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
    return db


def carregar_do_banco() -> tuple[list[dict], list[dict]]:
    """(catálogo, itens) no formato que `motor.parear` consome.

    O item entra com a `descricao_final` da etapa 5 quando existe, e com a descrição da API
    quando não — mesma precedência do caminho CSV. Só entram itens `manter`: item marcado
    `descartar` pela extração não deve ocupar espaço no produto cartesiano.
    """
    db = _exigir_banco()
    with db.session() as s:
        cat = [{"codigo": c, "texto": f"{n or ''} {d or ''}".strip(), "categoria": cat_}
               for c, n, d, cat_ in s.execute(sa_text(
                   "SELECT codigo, nome_pdm, description, categoria FROM catalogo_item "
                   " WHERE active AND coalesce(categoria, '') <> ''")).all()]
        itens = [{"item_key": k, "descricao_final": d, "categoria": cat_}
                 for k, d, cat_ in s.execute(sa_text("""
                     SELECT i.item_key,
                            coalesce(NULLIF(e.descricao_final, ''), i.descricao_api),
                            ic.categoria
                       FROM item i
                       JOIN item_categoria ic USING (item_key)
                       LEFT JOIN item_enriquecido e USING (item_key)
                      WHERE i.sobrevivente
                        AND coalesce(e.destino::text, 'manter') <> 'descartar'
                 """)).all()]
    if not cat:
        raise SystemExit("catalogo_item sem categoria — rode a etapa 1 antes.")
    if not itens:
        raise SystemExit("Nenhum item sobrevivente com categoria — rode as etapas 3 e 4 antes.")
    return cat, itens


def run(params: Params, ctx: RunContext) -> StepResult:
    db = _exigir_banco()
    from pesquisa_precos.db.repos import catalogo as repo_cat
    from pesquisa_precos.db.repos import par as repo_par

    catalogo, itens = carregar_do_banco()
    ctx.log("info", f"[6a] {len(catalogo)} códigos × {len(itens)} itens-categoria "
                    f"(produto restrito à mesma categoria)")

    provider = ctx.providers.matching
    onde = "serviço externo" if getattr(provider.info, "base_url", "") else "em processo"
    ctx.log("info", f"[6a] pareamento: {onde}")

    pares = provider.parear(catalogo, itens, piso=params.piso, top_k=params.top_k)
    ctx.log("info", f"[6a] {len(pares)} pares sobreviventes (piso={params.piso}, "
                    f"top_k={params.top_k})")

    # `par.tipo` é parte da PK do catálogo (o código só é único DENTRO do tipo); o motor não
    # conhece essa distinção, então o tipo é resolvido aqui, uma vez, para todos os pares.
    with db.session() as s:
        tipo_por_codigo, ambiguos = repo_cat.tipo_do_codigo(s)
    if ambiguos:
        ctx.log("aviso", f"[yellow][6a] {len(ambiguos)} códigos existem nos DOIS tipos — "
                         f"o par vai para o primeiro encontrado.[/]")

    lote = [(p["par_key"], tipo_por_codigo.get(p["codigo"], "material"), p["codigo"],
             p["item_key"], p["categoria"], p["score_bm25"], p["score_cosseno"], True, None)
            for p in pares]
    with db.raw_connection() as conn:
        gravados = repo_par.gravar_candidatos(conn, lote)
        conn.commit()

    with db.session() as s:
        contagens = repo_par.contar(s)
    ctx.log("info", f"[bold green][6a] Gravados {gravados} pares[/] → tabela `par` "
                    f"({contagens})")

    return StepResult(
        processed=len(pares), erros=0,
        metrics={"sobreviventes": len(pares), "codigos": len(catalogo),
                  "itens_categoria": len(itens), **contagens},
        preview=pares[:50],
    )


def estimate(params: Params, ctx: RunContext) -> Estimate:
    """Custo é GPU/CPU, não token. A unidade útil é o par candidato (produto por categoria)."""
    from collections import Counter

    from pesquisa_precos.db import session as db

    ok, detalhe = db.is_available()
    if not ok:
        return Estimate(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    try:
        catalogo, itens = carregar_do_banco()
    except SystemExit as e:
        return Estimate(detalhes={"aviso": str(e)})
    # O produto é POR CATEGORIA: somar `len(cat) * len(itens)` daria um número
    # astronomicamente maior que o real e assustaria à toa.
    n_cod = Counter(c["categoria"] for c in catalogo)
    n_itn = Counter(i["categoria"] for i in itens)
    comuns = set(n_cod) & set(n_itn)
    pares = sum(n_cod[c] * n_itn[c] for c in comuns)
    return Estimate(
        unidades=pares, chamadas_llm=0, cost_usd=0.0,
        detalhes={"categorias": len(comuns),
                  "codigos": sum(n_cod[c] for c in comuns),
                  "itens_explodidos": sum(n_itn[c] for c in comuns),
                  "teto_de_sobreviventes (top-K)": sum(n_cod[c] for c in comuns) * params.top_k},
    )

