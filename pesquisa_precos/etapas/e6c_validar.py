"""
Etapa 6c — LLM só na faixa ambígua do reranker + acúmulo de rótulos.

Só os pares `ambiguo` da 6b chegam aqui (tipicamente a minoria: 57k de 250k no acervo atual).
Cada um vai ao LLM via comparar_par. Além disso, TODA decisão final — aceites/rejeições do 6b
por threshold extremo E os vereditos do 6c — é appendada em data/6_rotulos_acumulados.csv, que
cresce entre execuções e serve para recalibrar thresholds / futuramente fine-tunar o reranker.

⚠ RESTRIÇÃO DE CUSTO Nº 1 (ADR-004). Até a Fase 0 esta etapa usava o modelo CARO por padrão e
só usava o barato com `--fraco` — comportamento seguro dependia de alguém lembrar de digitar
uma flag. Aqui isso está invertido: **o modelo barato (PASS1) é o padrão** e o caro exige
`--forte` explícito. `--fraco` continua aceito, sem efeito, para não quebrar o comando que já
está no histórico do terminal do operador.

Entrada: data/6b_pares_rerankeados.csv (filtro ambiguo) + textos.
Saídas: data/6c_pares_validados.csv (par_key, mesmo_item, justificativa),
        data/6_rotulos_acumulados.csv (append, sem duplicar par_key já registrado).
Chave de resumo: par_key. Erros: erros/6c_erros.csv.

NÃO fazer: truncar 6_rotulos_acumulados.csv — é o ativo de calibração do projeto.

Uso: python -m pesquisa_precos.etapas.e6c_validar [--provedor openrouter] [--limite N]
"""

import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from pydantic import BaseModel, Field

from pesquisa_precos.config import paths
from pesquisa_precos.config.settings import custo_por_chamada, exigir
from pesquisa_precos.core.io_seguro import EscritorSeguro, ler_chaves_concluidas
from pesquisa_precos.core.paralelo import executar_paralelo
from pesquisa_precos.core.textos import descricao_itens, texto_catalogo
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa
from pesquisa_precos.providers.llm_curador import Curador

CHAVE = "6c"
VERSAO_CODIGO = "1.0.0"

RERANK = paths.E6B_RERANKEADOS
CATALOGO = paths.E0A_CATALOGO
SOBREVIVENTES = paths.E4_SOBREVIVENTES
ENRIQUECIDOS = paths.E5_ENRIQUECIDOS
VALIDADOS = paths.E6C_VALIDADOS
ROTULOS = paths.E6_ROTULOS
ERROS = paths.ERROS_6C

COLS_VALID = ["par_key", "mesmo_item", "justificativa"]
COLS_ROTULO = ["par_key", "texto_catalogo", "texto_item", "score_rerank", "decisao_final",
               "origem", "timestamp"]


class Params(BaseModel):
    provedor: str = Field("openrouter", description="Provedor de LLM [local|openrouter]")
    limite: int | None = Field(None, description="Teto de pares ambíguos a validar (debug)")
    concurrency: int = Field(4, ge=1, le=32, description="Chamadas simultâneas ao LLM")
    forte: bool = Field(
        False, description="Usa o modelo CARO (PASS2). Padrão é o barato — ver ADR-004.")
    fraco: bool = Field(
        False, description="(obsoleta) O modelo barato já é o padrão; a flag não faz nada.")


def _pendentes(params: Params) -> tuple[pd.DataFrame, list, set]:
    """(rerankeados, ambíguos ainda não validados, par_keys já validadas)."""
    df = pd.read_csv(RERANK, dtype=str, encoding="utf-8").fillna("")
    ambiguos = df[df["decisao"] == "ambiguo"]
    feitas = ler_chaves_concluidas(str(VALIDADOS), "par_key")
    pend = [r for _, r in ambiguos.iterrows() if r["par_key"] not in feitas]
    if params.limite:
        pend = pend[: params.limite]
    return df, pend, feitas


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Uma chamada por par ambíguo ainda não validado."""
    if not RERANK.exists():
        return Estimativa(detalhes={"aviso": f"{RERANK} ausente — rode a etapa 6b antes."})
    df, pend, feitas = _pendentes(params)
    n = len(pend)
    preco = custo_por_chamada(ctx.config, params.provedor, forte=params.forte)
    modelo = ctx.config["model_pass2"] if params.forte else ctx.config["model_pass1"]
    return Estimativa(
        unidades=n, chamadas_llm=n,
        custo_usd=None if preco is None else n * preco,
        duracao_s=n / max(params.concurrency, 1) * 2,
        detalhes={"pares_rerankeados": len(df),
                  "ambiguos": int((df["decisao"] == "ambiguo").sum()),
                  "já_validados": len(feitas),
                  "modelo": f"{modelo} ({'CARO' if params.forte else 'barato'})"},
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    cfg = ctx.config
    forte = params.forte
    if params.fraco:
        ctx.log("debug", "[dim][6c] --fraco é obsoleta: o modelo barato já é o padrão.[/]")
    msg = exigir(cfg, params.provedor)
    if msg:
        raise SystemExit(msg)
    if not RERANK.exists():
        raise SystemExit(f"{RERANK} ausente. Rode a etapa 6b antes.")

    df, pend, feitas = _pendentes(params)
    cat = texto_catalogo(str(CATALOGO))
    itens = descricao_itens(str(SOBREVIVENTES), str(ENRIQUECIDOS))

    def t_cat(par_key):
        return cat.get(par_key.split("::", 1)[0], {}).get("texto", "")

    def t_itm(par_key):
        return itens.get(par_key.split("::", 1)[1], "")

    # ── Acúmulo de rótulos: registra as decisões extremas do 6b (origem=rerank) ──
    rotulos_existentes = ler_chaves_concluidas(str(ROTULOS), "par_key")
    esc_rot = EscritorSeguro(str(ROTULOS), COLS_ROTULO)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    novos_rotulos = 0
    for _, r in df.iterrows():
        if r["decisao"] in ("aceito", "rejeitado") and r["par_key"] not in rotulos_existentes:
            esc_rot.escrever({
                "par_key": r["par_key"], "texto_catalogo": t_cat(r["par_key"])[:500],
                "texto_item": t_itm(r["par_key"])[:500], "score_rerank": r.get("score_rerank", ""),
                "decisao_final": r["decisao"], "origem": "rerank", "timestamp": ts,
            })
            rotulos_existentes.add(r["par_key"])
            novos_rotulos += 1

    # ── LLM nos ambíguos (resumível por par_key em 6c_pares_validados) ──
    n_ambiguos = int((df["decisao"] == "ambiguo").sum())
    ctx.log("info", f"[6c] Ambíguos: {n_ambiguos} | a validar por LLM: {len(pend)}")

    n_ok = [0]
    n_erros = [0]
    vereditos: dict[str, int] = {"sim": 0, "nao": 0}
    if pend:
        modelo = cfg["model_pass2"] if forte else cfg["model_pass1"]
        ctx.log("info" if not forte else "aviso",
                f"[6c] modelo de validação: {modelo} "
                f"({'FORTE/CARO — ver ADR-004' if forte else 'barato (padrão)'})")
        curador = Curador.from_provedor(cfg, params.provedor, forte=forte, max_retries=6)
        esc_val = EscritorSeguro(str(VALIDADOS), COLS_VALID)

        def fn(row):
            return curador.comparar_par(t_cat(row["par_key"]), t_itm(row["par_key"]))

        def ok(row, res):
            n_ok[0] += 1
            esc_val.escrever({"par_key": row["par_key"], "mesmo_item": res["mesmo_item"],
                              "justificativa": res["justificativa"]})
            if res["mesmo_item"] in ("sim", "nao") and row["par_key"] not in rotulos_existentes:
                vereditos[res["mesmo_item"]] += 1
                esc_rot.escrever({
                    "par_key": row["par_key"], "texto_catalogo": t_cat(row["par_key"])[:500],
                    "texto_item": t_itm(row["par_key"])[:500],
                    "score_rerank": row.get("score_rerank", ""),
                    "decisao_final": "aceito" if res["mesmo_item"] == "sim" else "rejeitado",
                    "origem": "llm", "timestamp": ts,
                })
                rotulos_existentes.add(row["par_key"])

        def err(row, exc):
            n_erros[0] += 1
            ctx.erro_item(row["par_key"], exc)

        ctx.progresso(0, len(pend), descricao="validando ambíguos")
        executar_paralelo(pend, fn, concurrency=params.concurrency, on_result=ok, on_error=err,
                          on_progress=lambda f, t: ctx.progresso(f, t))
        esc_val.fechar()

    esc_rot.fechar()
    ctx.log("info", f"[6c] Concluído. Validados: {VALIDADOS} | rótulos: {ROTULOS}")

    return ResultadoEtapa(
        processados=n_ok[0], erros=n_erros[0],
        metricas={"ambiguos": n_ambiguos, "validados_agora": n_ok[0],
                  "veredito_sim": vereditos["sim"], "veredito_nao": vereditos["nao"],
                  "rotulos_novos_do_rerank": novos_rotulos,
                  "modelo": "PASS2 (caro)" if forte else "PASS1 (barato)"},
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
