"""
Etapa 4 — Filtro de classificados: quem tem ≥1 categoria de conteúdo sobrevive. Sem LLM.

A etapa inteira é um UPDATE. "Sobrevivente" é ATRIBUTO do item (`item.sobrevivente`), não um
conjunto à parte: não existe tabela de sobreviventes para sair de sincronia com `item`. Mantém
TODAS as caixas — a antiga regra dos 5 (descartar categoria com < MIN_ITENS) foi removida; a
contagem por caixa é só diagnóstico.

Entradas: `item`, `item_categoria` (etapas 2 e 3). Saída: `item.sobrevivente`.
Chave de resumo: nenhuma — recomputa o corpus inteiro (é barato e o resultado depende de tudo).

NÃO fazer: reintroduzir descarte por MIN_ITENS aqui (ADR-016 — a "regra dos 5" está
desativada de propósito; mais de 5 itens por código é comportamento esperado).
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from pydantic import BaseModel

from pesquisa_precos.steps.base import RunContext, Estimate, StepResult

KEY = "4"
# 2.0.0 (Fase 13): o caminho CSV saiu — o banco é o único meio de persistência (ADR-020).
CODE_VERSION = "2.0.0"


class Params(BaseModel):
    pass


def run(params: Params, ctx: RunContext) -> StepResult:
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import documento as repo

    ok, detalhe = db.is_available()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")

    with db.session() as s:
        contagens = repo.contar(s)
        if not contagens["item"]:
            raise SystemExit("Nenhum item no banco. Rode a etapa 2 antes.")
        if not contagens["documento_termo"] and not contagens["item"]:
            raise SystemExit("Nada classificado. Rode a etapa 3 antes.")
        resultado = repo.marcar_sobreviventes_por_categoria(s)
        n_docs = repo.recontar_sobreviventes_por_documento(s)
        relatorio = repo.relatorio_por_categoria(s)
        s.commit()
        final = repo.contar(s)

    ctx.log("info", f"[4] Categorias mantidas: {len(relatorio)} "
                    f"(regra dos 5 desativada — ADR-016)")
    ctx.log("info", f"[bold green][4] Itens sobreviventes: "
                    f"{final['item_sobrevivente']}[/] "
                    f"(+{resultado['marcados']}, -{resultado['desmarcados']}) · "
                    f"{n_docs} documentos recontados")

    return StepResult(
        processed=final["item_sobrevivente"], errors=0,
        metrics={"categorias_mantidas": len(relatorio),
                  "itens_sobreviventes": final["item_sobrevivente"],
                  "documentos_recontados": n_docs, **resultado},
        preview=relatorio[:50],
    )


def estimate(params: Params, ctx: RunContext) -> Estimate:
    """Sem LLM: só conta quantos itens classificados entrariam no corte."""
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import classification as repo_cls
    from pesquisa_precos.db.repos import documento as repo

    ok, detalhe = db.is_available()
    if not ok:
        return Estimate(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    with db.session() as s:
        contagens = repo.contar(s)
        cls = repo_cls.contar(s)
    return Estimate(
        unidades=contagens["item"], chamadas_llm=0,
        detalhes={"itens_no_banco": contagens["item"],
                  "ligações item×categoria": cls.get("item_categoria", 0),
                  "sobreviventes_hoje": contagens["item_sobrevivente"]},
    )
