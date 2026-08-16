"""
Etapa 4 — Junção classificação × metadados + filtro de classificados (pandas puro, sem LLM).

Junta a classificação (etapa 3, só itens com ≥1 categoria) com os itens coletados, explode
o multi-label (1 linha por (item_key, categoria)) e reagrega. Mantém TODAS as caixas — a
antiga regra dos 5 (descartar categoria com < MIN_ITENS) foi removida. O único filtro é
"item tem ≥1 categoria de conteúdo"; a contagem por caixa vira apenas diagnóstico.

Entradas: data/2_itens_coletados.csv, data/3_itens_classificados.csv.
Saídas: data/4_itens_sobreviventes.csv (colunas da etapa 2 + categorias do item),
        data/4_relatorio_corte.csv (categoria, n_itens_coletados, mantida≡True) — diagnóstico.
Chave de resumo: nenhuma — recomputa o corpus inteiro (é barato e o resultado depende de tudo).

NÃO fazer: reintroduzir descarte por MIN_ITENS aqui (ADR-016 — a "regra dos 5" está
desativada de propósito; mais de 5 itens por código é comportamento esperado).

Uso: python -m pesquisa_precos.etapas.e4_cortar
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from pydantic import BaseModel, Field

from pesquisa_precos.config import paths
from pesquisa_precos.core.coleta import coleta_pncp
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa

CHAVE = "4"
VERSAO_CODIGO = "1.0.0"

ITENS = paths.E2_ITENS
CK_EXTRA = paths.CK_2_CONCEITOS_EXTRA
CLASSIF = paths.E3_CLASSIFICADOS
SOBREVIVENTES = paths.E4_SOBREVIVENTES
RELATORIO = paths.E4_RELATORIO


class Params(BaseModel):
    entrada_legado: str | None = Field(None, description="CSV explodido da v1 (aceite fase 2)")


def carregar_base(entrada_legado: str | None) -> pd.DataFrame:
    if entrada_legado:
        base = pd.read_csv(entrada_legado, dtype=str, encoding="utf-8-sig").fillna("")
        ctrl = base.get("numero_controle_pncp", "")
        num = base.get("item.numero_item", "")
        base = base.assign(item_key=[coleta_pncp.montar_item_key(c, n) for c, n in zip(ctrl, num)])
        return base.drop_duplicates(subset="item_key", keep="first")
    return coleta_pncp.carregar_itens_coletados(str(ITENS), str(CK_EXTRA))


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Sem LLM: só conta quantos itens classificados entrariam no corte."""
    if not CLASSIF.exists():
        return Estimativa(detalhes={"aviso": f"{CLASSIF} ausente — rode a etapa 3 antes."})
    clas = pd.read_csv(CLASSIF, dtype=str, encoding="utf-8").fillna("")
    com_categoria = clas[clas["categorias"].str.strip() != ""]
    return Estimativa(
        unidades=len(com_categoria), chamadas_llm=0,
        detalhes={"itens_classificados": len(clas),
                  "sem_categoria (morrem aqui)": len(clas) - len(com_categoria)},
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    base = carregar_base(params.entrada_legado)
    if base.empty:
        raise SystemExit("Sem itens coletados. Rode a etapa 2 antes (ou use --entrada-legado).")

    if not CLASSIF.exists():
        raise SystemExit(f"{CLASSIF} ausente. Rode a etapa 3 antes.")
    clas = pd.read_csv(CLASSIF, dtype=str, encoding="utf-8").fillna("")
    clas = clas[clas["categorias"].str.strip() != ""]

    # explode multi-label
    clas = clas.assign(categoria=clas["categorias"].str.split("|")).explode("categoria")
    clas = clas[clas["categoria"].str.strip() != ""]

    contagem = clas.groupby("categoria")["item_key"].nunique()
    # Regra dos 5 removida: mantém TODAS as caixas classificadas, independente da contagem.
    # O único filtro aqui é "item tem ≥1 caixa" (feito acima). A contagem vira só diagnóstico.
    mantidas = set(contagem.index)

    relatorio = (
        contagem.rename("n_itens_coletados").reset_index()
        .assign(mantida=lambda d: d["categoria"].isin(mantidas))
        .sort_values("n_itens_coletados", ascending=False)
    )
    relatorio.to_csv(RELATORIO, index=False, encoding="utf-8")

    # sobreviventes: itens com ≥1 categoria mantida; categorias filtradas às mantidas.
    itens_cat = (
        clas[clas["categoria"].isin(mantidas)]
        .groupby("item_key")["categoria"].apply(lambda s: "|".join(sorted(set(s))))
        .rename("categorias").reset_index()
    )
    base_cols = [c for c in base.columns if c != "categorias"]
    sobrev = itens_cat.merge(base[base_cols], on="item_key", how="left")
    sobrev.to_csv(SOBREVIVENTES, index=False, encoding="utf-8")

    ctx.log("info", f"[4] Categorias mantidas: {sorted(mantidas)}")
    ctx.log("info", f"[4] Itens sobreviventes: {len(sobrev)} | relatório: {RELATORIO}")
    ctx.log("info", f"[4] Saída: {SOBREVIVENTES}")

    return ResultadoEtapa(
        processados=len(sobrev), erros=0,
        metricas={"categorias_mantidas": len(mantidas), "itens_sobreviventes": len(sobrev)},
        preview=relatorio.head(50).to_dict("records"),
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
