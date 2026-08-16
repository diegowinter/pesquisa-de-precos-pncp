"""
Etapa 3 — Classificação de categoria por item do PNCP (LLM, multi-label, O(textos)).

DEDUP: a descrição do PNCP é canônica e se repete MUITO (o mesmo texto reaparece milhares de
vezes). Classificamos cada texto ÚNICO (descrição, unidade) uma vez e espalhamos o rótulo para
todos os item_keys iguais — a saída continua por item_key (referência à ata/contrato intacta),
mas as chamadas de LLM caem de O(itens) para O(textos distintos). Item sem categoria de conteúdo
morre aqui (a "portaria de nomeação" nunca mais custa nada nas etapas seguintes).

Entrada: data/2_itens_coletados.csv (via coleta_pncp.carregar_itens_coletados). Para o aceite
sobre dados legados, use --entrada-legado com um CSV explodido da v1 (mapeia
item.descricao_item / numero_controle_pncp+item.numero_item).

Saída: data/3_itens_classificados.csv (item_key, categorias, confianca). Erros: erros/3_erros.csv.
Chave de resumo: item_key.

NÃO fazer: classificar por item em vez de por texto único — é o dedup de ~5x que segura o
custo desta etapa, a mais cara do ciclo.

Uso: python -m pesquisa_precos.etapas.e3_classificar [--provedor local|openrouter] [--limite N]
"""

import sys
import threading

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from pydantic import BaseModel, Field

from pesquisa_precos.config import paths
from pesquisa_precos.config.settings import custo_por_chamada, exigir
from pesquisa_precos.core.coleta import coleta_pncp
from pesquisa_precos.core.io_seguro import (
    EscritorSeguro,
    escrever_csv,
    ler_chaves_concluidas,
    ler_csv,
)
from pesquisa_precos.core.paralelo import executar_paralelo
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa
from pesquisa_precos.providers.llm_curador import Curador

CHAVE = "3"
VERSAO_CODIGO = "1.0.0"

ENTRADA = paths.E2_ITENS
CK_EXTRA = paths.CK_2_CONCEITOS_EXTRA
SAIDA = paths.E3_CLASSIFICADOS
ERROS = paths.ERROS_3

COLS = ["item_key", "categorias", "confianca"]


class Params(BaseModel):
    provedor: str = Field("local", description="Provedor de LLM [local|openrouter]")
    limite: int | None = Field(None, description="Teto de textos únicos a classificar (debug)")
    concurrency: int = Field(3, ge=1, le=32, description="Chamadas simultâneas ao LLM")
    entrada_legado: str | None = Field(None, description="CSV explodido da v1 (aceite fase 2)")
    retry_erros: bool = Field(
        False, description="Reprocessa só as chaves de erros/3_erros.csv")
    reasoning: bool = Field(
        False, description="Mantém o raciocínio do modelo LIGADO. Padrão: desligado.")


def carregar_itens(entrada_legado: str | None) -> pd.DataFrame:
    if entrada_legado:
        df = pd.read_csv(entrada_legado, dtype=str, encoding="utf-8-sig").fillna("")
        ctrl = df.get("numero_controle_pncp", pd.Series([""] * len(df)))
        num = df.get("item.numero_item", pd.Series([""] * len(df)))
        df = df.assign(
            item_key=[coleta_pncp.montar_item_key(c, n) for c, n in zip(ctrl, num)],
            descricao_api=df.get("item.descricao_item", ""),
            unidade=df.get("item.unidade_medida", ""),
        ).drop_duplicates(subset="item_key", keep="first")
        return df[["item_key", "descricao_api", "unidade"]]
    df = coleta_pncp.carregar_itens_coletados(str(ENTRADA), str(CK_EXTRA))
    if df.empty:
        raise SystemExit(f"{ENTRADA} vazio/ausente. Rode a etapa 2 (ou use --entrada-legado).")
    return df[["item_key", "descricao_api", "unidade"]]


def _norm(s) -> str:
    return " ".join(str(s or "").strip().lower().split())


def agrupar_por_texto(pendentes: list) -> list[dict]:
    """DEDUP: a descrição do PNCP é canônica e se repete MUITO (o mesmo texto reaparece
    milhares de vezes — às vezes centenas dentro de um único contrato). Classificamos cada
    texto ÚNICO uma vez e ESPALHAMOS o rótulo para todos os item_keys daquele texto.
    A saída continua por item_key (referência à ata/contrato intacta); só as chamadas de LLM
    caem de O(itens) para O(textos distintos). Chave = (descrição, unidade) — os dois campos
    que o classificador usa, então texto igual ⇒ mesma classe, sem perda."""
    grupos: dict = {}
    for r in pendentes:
        chave = (_norm(r["descricao_api"]), _norm(r.get("unidade", "")))
        g = grupos.get(chave)
        if g is None:
            g = grupos[chave] = {"descricao_api": r["descricao_api"],
                                 "unidade": r.get("unidade", ""), "keys": []}
        g["keys"].append(r["item_key"])
    return list(grupos.values())


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Uma chamada por TEXTO único ainda não classificado — não por item."""
    if not params.entrada_legado and not ENTRADA.exists():
        return Estimativa(detalhes={"aviso": f"{ENTRADA} ausente — rode a etapa 2 antes."})
    df = carregar_itens(params.entrada_legado)
    feitas = ler_chaves_concluidas(str(SAIDA), "item_key")
    pend = [r for _, r in df.iterrows() if r["item_key"] not in feitas]
    tarefas = agrupar_por_texto(pend)
    n = len(tarefas) if not params.limite else min(len(tarefas), params.limite)
    preco = custo_por_chamada(ctx.config, params.provedor)
    return Estimativa(
        unidades=n, chamadas_llm=n,
        custo_usd=None if preco is None else n * preco,
        duracao_s=n / max(params.concurrency, 1) * 2,
        detalhes={"itens_pendentes": len(pend), "textos_unicos": len(tarefas),
                  "dedup": f"{len(pend) / max(len(tarefas), 1):.1f}x",
                  "itens_ja_classificados": len(feitas)},
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    cfg = ctx.config
    msg = exigir(cfg, params.provedor)
    if msg:
        raise SystemExit(msg)

    df = carregar_itens(params.entrada_legado)
    if params.retry_erros:
        # As linhas de erro foram gravadas na saída (confianca=erro) e contam como 'feitas',
        # então o filtro normal nunca as reprocessava (bug). Reescrevemos a saída SEM elas e
        # deixamos o fluxo normal reclassificar o que faltou — sem duplicar item_key.
        linhas = list(ler_csv(str(SAIDA)))
        limpas = [l for l in linhas if l.get("confianca") != "erro"]
        escrever_csv(str(SAIDA), COLS, limpas)
        ctx.log("info", f"[3] retry-erros: {len(linhas) - len(limpas)} linhas de erro removidas "
                        f"da saída para reprocessar.")
        feitas = {l["item_key"] for l in limpas}
    else:
        feitas = ler_chaves_concluidas(str(SAIDA), "item_key")
    pend = [r for _, r in df.iterrows() if r["item_key"] not in feitas]
    if not pend:
        ctx.log("info", "[3] Nada a classificar (tudo já feito).")
        return ResultadoEtapa(metricas={"itens_ja_classificados": len(feitas)})

    tarefas = agrupar_por_texto(pend)
    n_textos = len(tarefas)
    if params.limite:
        tarefas = tarefas[: params.limite]
    n_itens = sum(len(g["keys"]) for g in tarefas)
    limite_txt = f" — rodando só {len(tarefas)} (limite)" if params.limite else ""
    ctx.log("info", f"[bold][3] Dedup: {len(pend)} itens → {n_textos} textos únicos[/]"
                    f"{limite_txt}\n    classificando {len(tarefas)} textos ({n_itens} itens), "
                    f"já feitos: {len(feitas)}, concorrência: {params.concurrency}")

    # Um Curador por thread: compartilhar um único cliente ChatOpenAI serializa as chamadas
    # HTTP e mata a concorrência (ver etapa 1). Cada worker cria o seu (thread-local).
    #
    # Desligar o raciocínio depende do servidor: o LM Studio (local) só respeita
    # `reasoning_effort: "none"` (top-level); o formato `reasoning:{enabled:false}` do
    # OpenRouter ele IGNORA. Modelo de raciocínio ligado custa ~5x o tempo por item.
    reasoning_kw = {}
    if not params.reasoning:
        reasoning_kw = ({"extra_body": {"reasoning_effort": "none"}}
                        if params.provedor == "local" else {"reasoning": {"enabled": False}})
    ctx.log("debug", f"[dim][3] reasoning: "
                     f"{'ligado (default do modelo)' if params.reasoning else 'DESLIGADO'}[/]")
    _tls = threading.local()

    def _curador():
        if not hasattr(_tls, "c"):
            _tls.c = Curador.from_provedor(cfg, params.provedor, max_retries=6, **reasoning_kw)
        return _tls.c

    n_erros = [0]
    n_ok = [0]
    with EscritorSeguro(str(SAIDA), COLS) as w:
        # Cada tarefa é um GRUPO (texto único). Classifica uma vez e escreve o mesmo rótulo
        # para todos os item_keys do grupo (fan-out). As escritas rodam na thread principal
        # (ver paralelo.py), então gravar N item_keys de uma vez é seguro.
        def fn(g):
            return _curador().classificar_categoria(g["descricao_api"], g.get("unidade", ""))

        def ok(g, res):
            cats = "|".join(res["categorias"])
            conf = res.get("confianca", "")
            if conf == "erro":
                n_erros[0] += 1
                ctx.erro_item(g["keys"][0], res.get("_erro"), nome=g["descricao_api"])
            else:
                n_ok[0] += 1
            for k in g["keys"]:
                w.escrever({"item_key": k, "categorias": cats, "confianca": conf})

        def err(g, exc):
            n_erros[0] += 1
            ctx.erro_item(g["keys"][0], exc, nome=g["descricao_api"])
            for k in g["keys"]:
                w.escrever({"item_key": k, "categorias": "", "confianca": "erro"})

        ctx.progresso(0, len(tarefas), descricao="classificando")
        executar_paralelo(tarefas, fn, concurrency=params.concurrency,
                          on_result=ok, on_error=err,
                          on_progress=lambda f, t: ctx.progresso(f, t))
    cor = "yellow" if n_erros[0] else "green"
    ctx.log("info", f"[bold {cor}][3] Concluído.[/] {n_erros[0]} erros. → {SAIDA}")

    return ResultadoEtapa(
        processados=n_ok[0], erros=n_erros[0],
        metricas={"textos_unicos": len(tarefas), "itens_afetados": n_itens,
                  "dedup": f"{len(pend) / max(n_textos, 1):.1f}x"},
        preview=[{"descricao": g["descricao_api"][:200], "itens": len(g["keys"])}
                 for g in tarefas[:30]],
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
