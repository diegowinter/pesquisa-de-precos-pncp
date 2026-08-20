"""
Etapa 7 — Agrupar por código, sanity de preço e ranking por menor preço unitário.

Confirmados = pares `aceito` da 6b ∪ pares `mesmo_item=sim` da 6c. Antes do ranking, marca
outliers de preço por IQR (por código): preço < Q1-3*IQR ou > Q3+3*IQR → flag_preco=true
(fica no arquivo mas fora do ranking — um erro de unidade não pode contaminar a pesquisa). Se
existir data/config_faixas_preco.csv (categoria, preco_min, preco_max), aplica também. Por
código, conta confirmados não-flagados; < min_itens descarta o código. Nos que fecham, ordena
por preço unitário crescente e aplica top_n quando > 0.

Entradas (`--fonte csv`): data/6b_pares_rerankeados.csv, data/6c_pares_validados.csv,
sobreviventes/enriquecidos. Saídas: data/7_itens_agrupados.csv, data/7_relatorio_grupos.csv.
Chave de resumo: nenhuma — recomputa o corpus inteiro (comparar preço exige todos os itens).

FASE 2 — `--fonte banco` lê os pares confirmados de `par` (com `decisao_final='confirmado'`,
já derivada) e grava o ranking em `grupo_item`, carimbado com o `run_id`. A regra de negócio é
a MESMA nos dois caminhos: as funções `flag_iqr`, o corte por `min_itens` e o `top_n` não são
duplicadas — só muda de onde vêm as linhas e para onde vai o resultado. Foi assim de propósito:
duas implementações da regra de menor preço seriam duas verdades sobre o preço.

⚠ NÃO fazer: tratar `top_n = 0` como "zero itens". Zero significa SEM TETO — traz todas as
referências confirmadas não sinalizadas por código. Mais de 5 itens por código é o
comportamento esperado (ADR-016: a "regra dos 5" está desativada, `min_itens=1`, `top_n=0`).

Uso: python -m pesquisa_precos.etapas.e7_agrupar [--fonte banco|csv]
"""

import sys
from typing import Literal

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from pesquisa_precos.config import paths
from pesquisa_precos.core.textos import descricao_itens, texto_catalogo
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa

CHAVE = "7"
# 1.1.0 (Fase 2): ganhou `--fonte banco`. A regra de agrupamento não mudou — o fingerprint
# muda porque a origem dos dados passa a fazer parte dos params.
# 1.2.0 (Fase 10): `banco` vira o DEFAULT da etapa. A pipeline inteira passa a gravar
# no banco; `--fonte csv` fica como escape hatch para rodar fora do servidor.
VERSAO_CODIGO = "1.2.0"

RERANK = paths.E6B_RERANKEADOS
VALIDADOS = paths.E6C_VALIDADOS
SOBREVIVENTES = paths.E4_SOBREVIVENTES
ENRIQUECIDOS = paths.E5_ENRIQUECIDOS
CATALOGO = paths.E0A_CATALOGO
PARES = paths.E6A_PARES
FAIXAS = paths.FAIXAS_PRECO
AGRUPADOS = paths.E7_AGRUPADOS
RELATORIO = paths.E7_RELATORIO

META_ITEM = ["tipo_doc", "numeroControlePNCP", "numeroItem", "unidade", "quantidade",
             "preco_unitario", "preco_estimado", "fornecedor", "data_resultado",
             "orgao", "uf", "data", "ano", "orgao_cnpj",
             "data_fim_vigencia", "data_assinatura"]


class Params(BaseModel):
    # Defaults `None` = "usa o que está na configuração" (hoje o `.env`: MIN_ITENS / TOP_N).
    # Na Fase 6 esses valores passam a vir de `config_valor` versionado, sem mudar a etapa.
    min_itens: int | None = Field(
        None, ge=1, description="Mín. de confirmados p/ o código fechar (default: config)")
    top_n: int | None = Field(
        None, ge=0, description="Máx. de itens por código; 0 = SEM TETO (default: config)")
    fator_iqr: float = Field(
        3.0, gt=0, description="Multiplicador do IQR na marcação de outlier de preço")
    fonte: Literal["banco", "csv"] = Field(
        "banco", description="De onde vêm os pares confirmados e para onde vai o resultado")
    run: str = Field(
        "corrente", description="Rótulo do run que carimba grupo_item (só com --fonte banco)")


def confirmados_do_banco(rotulo_run: str) -> tuple[pd.DataFrame, int]:
    """Pares confirmados + metadados do item, direto do banco. Devolve (df, run_id).

    Um SELECT só, no repositório — a etapa não escreve SQL (regra da Fase 2). As colunas
    saem com os MESMOS nomes que o caminho CSV usa (`numeroControlePNCP`, `numeroItem`…),
    para que todo o resto da função `executar` seja literalmente o mesmo código. Renomear
    aqui é mais barato que manter duas versões da regra de menor preço.
    """
    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import execucao as repo_exec
    from pesquisa_precos.db.repos import par as repo_par

    ok, detalhe = db.esta_disponivel()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env "
                         f"ou rode com --fonte csv.")
    with db.sessao() as s:
        linhas = repo_par.confirmados(s)
        run_id = repo_exec.run_aberto_ou_criar(s, rotulo_run)
    if not linhas:
        return pd.DataFrame(), run_id

    df = pd.DataFrame(linhas).rename(columns={
        "numero_controle_pncp": "numeroControlePNCP",
        "numero_item": "numeroItem",
    })
    # O resto da etapa trabalha com texto (o caminho CSV lê tudo como str). Preço é a
    # exceção: continua Decimal até virar `preco_num`, para não passar por float duas vezes.
    for coluna in df.columns:
        if coluna not in ("preco_unitario", "preco_estimado", "quantidade"):
            df[coluna] = df[coluna].map(lambda v: "" if v is None else str(v))
    return df, run_id


def confirmados() -> pd.DataFrame:
    if not RERANK.exists():
        raise SystemExit(f"{RERANK} ausente. Rode a etapa 6b antes.")
    rer = pd.read_csv(RERANK, dtype=str, encoding="utf-8").fillna("")
    keys = set(rer[rer["decisao"] == "aceito"]["par_key"])
    if VALIDADOS.exists():
        val = pd.read_csv(VALIDADOS, dtype=str, encoding="utf-8").fillna("")
        keys |= set(val[val["mesmo_item"] == "sim"]["par_key"])
    cat_map = {}
    if PARES.exists():
        pares = pd.read_csv(PARES, dtype=str, encoding="utf-8").fillna("")
        cat_map = dict(zip(pares["par_key"], pares["categoria"]))
    linhas = []
    for pk in keys:
        codigo, item_key = pk.split("::", 1)
        linhas.append({"par_key": pk, "codigo": codigo, "item_key": item_key,
                       "categoria": cat_map.get(pk, "")})
    return pd.DataFrame(linhas)


def flag_iqr(precos: pd.Series, fator: float = 3.0) -> pd.Series:
    p = pd.to_numeric(precos, errors="coerce")
    q1, q3 = p.quantile(0.25), p.quantile(0.75)
    iqr = q3 - q1
    lo, hi = q1 - fator * iqr, q3 + fator * iqr
    # preço ausente ou <= 0 é lixo (não preenchido no PNCP): flaga sempre, mesmo que o IQR não pegue.
    return (p.isna()) | (p <= 0) | (p < lo) | (p > hi)


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Sem LLM. A unidade é o par confirmado que entra no agrupamento."""
    min_itens = params.min_itens if params.min_itens is not None else ctx.config["min_itens"]
    top_n = params.top_n if params.top_n is not None else ctx.config["top_n"]
    comum = {"min_itens": min_itens, "top_n": f"{top_n} (0 = sem teto)",
             "fonte": params.fonte}

    if params.fonte == "banco":
        from pesquisa_precos.db import sessao as db
        from pesquisa_precos.db.repos import par as repo_par

        ok, detalhe = db.esta_disponivel()
        if not ok:
            return Estimativa(detalhes={**comum, "aviso": f"banco indisponível: {detalhe}"})
        with db.sessao() as s:
            n = repo_par.contar(s)["par_confirmado"]
        return Estimativa(unidades=n, chamadas_llm=0, custo_usd=0.0, detalhes=comum)

    if not RERANK.exists():
        return Estimativa(detalhes={**comum,
                                    "aviso": f"{RERANK} ausente — rode a etapa 6b antes."})
    conf = confirmados()
    return Estimativa(
        unidades=len(conf), chamadas_llm=0, custo_usd=0.0,
        detalhes={**comum,
                  "codigos_distintos": conf["codigo"].nunique() if not conf.empty else 0},
    )


def carregar_confirmados(params: Params, ctx: ContextoExecucao) -> tuple[pd.DataFrame, int | None]:
    """Pares confirmados enriquecidos, vindos do banco ou dos CSVs. (df, run_id).

    As duas origens entregam as MESMAS colunas — é isso que permite que o miolo da etapa
    (flag de preço, corte por `min_itens`, `top_n`) exista uma vez só.
    """
    if params.fonte == "banco":
        conf, run_id = confirmados_do_banco(params.run)
        if conf.empty:
            return conf, run_id
        from pesquisa_precos.db import sessao as db
        from pesquisa_precos.db.repos import catalogo as repo_cat
        with db.sessao() as s:
            catt = repo_cat.texto_por_codigo(s)
        conf["nome_catalogo"] = conf["codigo"].map(
            lambda c: catt.get(c, {}).get("nome_pdm", ""))
        ctx.log("info", f"[7] Fonte: banco (run #{run_id}) — {len(conf)} pares confirmados.")
        return conf, run_id

    conf = confirmados()
    if conf.empty:
        return conf, None
    sob = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    meta_cols = ["item_key"] + [c for c in META_ITEM if c in sob.columns]
    conf = conf.merge(sob[meta_cols], on="item_key", how="left")

    desc = descricao_itens(str(SOBREVIVENTES), str(ENRIQUECIDOS))
    conf["descricao_final"] = conf["item_key"].map(desc).fillna("")
    catt = texto_catalogo(str(CATALOGO))
    conf["nome_catalogo"] = conf["codigo"].map(lambda c: catt.get(c, {}).get("nome", ""))
    return conf, None


def carregar_faixas(params: Params) -> dict[str, tuple[float, float]]:
    """`categoria → (min, max)`, com `nan` onde não há limite daquele lado.

    Limite vazio significa SEM limite, não zero — `arma_fogo,5,` é "mínimo 5, sem teto".
    Tratar o vazio como 0 sinalizaria a categoria inteira como fora de faixa.
    """
    if params.fonte == "banco":
        from pesquisa_precos.db import sessao as db
        from pesquisa_precos.db.repos import grupo as repo_grupo
        with db.sessao() as s:
            return {c: (float(lo) if lo is not None else np.nan,
                        float(hi) if hi is not None else np.nan)
                    for c, (lo, hi) in repo_grupo.faixas(s).items()}
    if not FAIXAS.exists():
        return {}
    fx = pd.read_csv(FAIXAS, dtype=str, encoding="utf-8").fillna("")
    return {r["categoria"]: (float(r["preco_min"] or "nan"), float(r["preco_max"] or "nan"))
            for _, r in fx.iterrows()}


def gravar_no_banco(selec: pd.DataFrame, run_id: int, ctx: ContextoExecucao) -> int:
    """Ranking → `grupo_item`, com `posicao` recontada por código na ordem já ordenada.

    Limpa o run antes: reexecutar a 7 no MESMO run sem limpar deixaria posições de um corte
    que não existe mais (ver `repos/grupo.limpar_run`). Runs anteriores ficam intactos —
    é o append-only por `run_id` do ADR-015.
    """
    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.copia import em_lotes
    from pesquisa_precos.db.repos import grupo as repo_grupo

    with db.sessao() as s:
        apagadas = repo_grupo.limpar_run(s, run_id)
    if apagadas:
        ctx.log("info", f"[7] run #{run_id} já tinha {apagadas} linhas — substituídas.")

    posicao: dict[str, int] = {}
    linhas = []
    for r in selec.to_dict("records"):
        codigo = r.get("codigo", "")
        posicao[codigo] = posicao.get(codigo, 0) + 1
        linhas.append((r.get("tipo") or "material", codigo, r["item_key"], r["par_key"],
                       posicao[codigo], r.get("preco_unitario") or None,
                       bool(r.get("flag_preco")), None, run_id))

    gravadas = 0
    with db.conexao_bruta() as conn:
        for lote in em_lotes(linhas, 20_000):
            gravadas += repo_grupo.gravar(conn, lote)
            conn.commit()
    ctx.log("info", f"[7] {gravadas} linhas gravadas em grupo_item (run #{run_id}).")
    return gravadas


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    min_itens = params.min_itens if params.min_itens is not None else ctx.config["min_itens"]
    top_n = params.top_n if params.top_n is not None else ctx.config["top_n"]

    conf, run_id = carregar_confirmados(params, ctx)
    if conf.empty:
        raise SystemExit("Nenhum par confirmado (aceito/sim). Rode 6b/6c antes.")

    conf["preco_num"] = pd.to_numeric(conf.get("preco_unitario"), errors="coerce")
    conf["flag_preco"] = conf.groupby("codigo")["preco_unitario"].transform(
        flag_iqr, params.fator_iqr)

    # faixas manuais opcionais por categoria
    faixa = carregar_faixas(params)
    if faixa:
        def fora_faixa(row):
            lo, hi = faixa.get(row["categoria"], (np.nan, np.nan))
            p = row["preco_num"]
            return bool((not np.isnan(lo) and p < lo) or (not np.isnan(hi) and p > hi))
        conf["flag_preco"] = conf["flag_preco"] | conf.apply(fora_faixa, axis=1)

    n_flag_total = int(conf["flag_preco"].sum())
    ctx.log("info", f"[7] Piso/IQR cortou {n_flag_total} de {len(conf)} confirmados por preço "
                    f"absurdo/ausente ({'com' if faixa else 'sem'} faixas por categoria).")

    validos = conf[~conf["flag_preco"]].copy()
    contagem = validos.groupby("codigo")["item_key"].nunique()
    fecham = set(contagem[contagem >= min_itens].index)

    # Ordena por preço (mais baratos primeiro). top_n<=0 → traz TODAS as referências
    # confirmadas não-lixo por código (sem teto); top_n>0 → só as N mais baratas.
    selec = (
        validos[validos["codigo"].isin(fecham)]
        .sort_values("preco_num", kind="mergesort")
    )
    if top_n and top_n > 0:
        selec = selec.groupby("codigo", group_keys=False).head(top_n)

    if params.fonte == "banco":
        gravar_no_banco(selec, run_id, ctx)
    else:
        selec.drop(columns=["preco_num"], errors="ignore").to_csv(
            AGRUPADOS, index=False, encoding="utf-8")

    relatorio = pd.DataFrame({
        "codigo": contagem.index,
        "n_confirmados": contagem.values,
    })
    n_flag = conf[conf["flag_preco"]].groupby("codigo")["item_key"].nunique()
    relatorio["n_flagados"] = relatorio["codigo"].map(n_flag).fillna(0).astype(int)
    relatorio["fechou"] = relatorio["codigo"].isin(fecham)
    # O relatório continua em CSV nas duas fontes: é diagnóstico para o operador, não
    # produto — não vale uma tabela no banco antes de a interface da Fase 5 existir.
    relatorio.sort_values("n_confirmados", ascending=False).to_csv(
        RELATORIO, index=False, encoding="utf-8")

    teto = "sem teto" if not top_n or top_n <= 0 else f"top{top_n}"
    ctx.log("info", f"[7] Códigos que fecharam ({min_itens}+): {len(fecham)} | "
                    f"linhas ({teto}): {len(selec)}")
    destino = f"grupo_item (run #{run_id})" if params.fonte == "banco" else str(AGRUPADOS)
    ctx.log("info", f"[7] Saída: {destino} | relatório: {RELATORIO}")

    return ResultadoEtapa(
        processados=len(selec), erros=0,
        metricas={"confirmados": len(conf), "flagados_por_preco": n_flag_total,
                  "codigos_que_fecharam": len(fecham), "linhas_no_ranking": len(selec),
                  "min_itens": min_itens, "top_n": top_n,
                  "fonte": params.fonte, "run_id": run_id},
        preview=relatorio.sort_values("n_confirmados", ascending=False)
                         .head(50).to_dict("records"),
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
