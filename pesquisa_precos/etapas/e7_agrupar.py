"""
Etapa 7 — Agrupar por código, sanity de preço e ranking por menor preço unitário.

Confirmados = pares `aceito` da 6b ∪ pares `mesmo_item=sim` da 6c. Antes do ranking, marca
outliers de preço por IQR (por código): preço < Q1-3*IQR ou > Q3+3*IQR → flag_preco=true
(fica no arquivo mas fora do ranking — um erro de unidade não pode contaminar a pesquisa). Se
existir faixa curada em `faixa_preco` (categoria, preco_min, preco_max), aplica também. Por
código, conta confirmados não-flagados; < min_itens descarta o código. Nos que fecham, ordena
por preço unitário crescente e aplica top_n quando > 0.

Entrada: `par` com `decisao_final='confirmado'` (derivada da 6b/6c). Saída: `grupo_item`,
carimbado com o `run_id`.
Chave de resumo: nenhuma — recomputa o corpus inteiro (comparar preço exige todos os itens).

⚠ NÃO fazer: tratar `top_n = 0` como "zero itens". Zero significa SEM TETO — traz todas as
referências confirmadas não sinalizadas por código. Mais de 5 itens por código é o
comportamento esperado (ADR-016: a "regra dos 5" está desativada, `min_itens=1`, `top_n=0`).
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa

CHAVE = "7"
# 2.0.0 (Fase 13): o caminho CSV saiu — o banco é a única origem e o único destino
# (ADR-020). A regra de agrupamento em si nunca mudou.
VERSAO_CODIGO = "2.0.0"

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
    run: str = Field(
        "corrente", description="Rótulo do run que carimba grupo_item")


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
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
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
    comum = {"min_itens": min_itens, "top_n": f"{top_n} (0 = sem teto)"}

    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import par as repo_par

    ok, detalhe = db.esta_disponivel()
    if not ok:
        return Estimativa(detalhes={**comum, "aviso": f"banco indisponível: {detalhe}"})
    with db.sessao() as s:
        n = repo_par.contar(s)["par_confirmado"]
    return Estimativa(unidades=n, chamadas_llm=0, custo_usd=0.0, detalhes=comum)


def carregar_confirmados(params: Params, ctx: ContextoExecucao) -> tuple[pd.DataFrame, int | None]:
    """Pares confirmados já enriquecidos com metadados do item. (df, run_id)."""
    conf, run_id = confirmados_do_banco(params.run)
    if conf.empty:
        return conf, run_id
    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import catalogo as repo_cat
    with db.sessao() as s:
        catt = repo_cat.texto_por_codigo(s)
    conf["nome_catalogo"] = conf["codigo"].map(
        lambda c: catt.get(c, {}).get("nome_pdm", ""))
    ctx.log("info", f"[7] run #{run_id} — {len(conf)} pares confirmados.")
    return conf, run_id


def carregar_faixas(params: Params) -> dict[str, tuple[float, float]]:
    """`categoria → (min, max)`, com `nan` onde não há limite daquele lado.

    Limite vazio significa SEM limite, não zero — `arma_fogo,5,` é "mínimo 5, sem teto".
    Tratar o vazio como 0 sinalizaria a categoria inteira como fora de faixa.
    """
    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import grupo as repo_grupo
    with db.sessao() as s:
        return {c: (float(lo) if lo is not None else np.nan,
                    float(hi) if hi is not None else np.nan)
                for c, (lo, hi) in repo_grupo.faixas(s).items()}


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

    gravar_no_banco(selec, run_id, ctx)
