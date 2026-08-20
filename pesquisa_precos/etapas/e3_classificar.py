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
from typing import Literal

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
from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.core.textos import texto_hash
from pesquisa_precos.db import sessao as db
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa

CHAVE = "3"
# 1.1.0 (Fase 2): o dedup passa a agrupar pelo `texto_hash` canônico de core.textos, que
# dobra acento — antes o agrupamento era por (lower, espaços colapsados) sem dobra.
VERSAO_CODIGO = "1.1.0"

ENTRADA = paths.E2_ITENS
CK_EXTRA = paths.CK_2_CONCEITOS_EXTRA
SAIDA = paths.E3_CLASSIFICADOS
ERROS = paths.ERROS_3

COLS = ["item_key", "categorias", "confianca"]


class Params(BaseModel):
    provedor: str | None = Field(
        None, description="Override manual do provedor de chat [local|openrouter]. "
        "Sem valor, usa o que estiver configurado em capacidade_provedor (Fase 7) — ou "
        "'local' se o banco de provedores ainda não tiver sido configurado (ADR-014).")
    limite: int | None = Field(None, description="Teto de textos únicos a classificar (debug)")
    concurrency: int = Field(3, ge=1, le=32, description="Chamadas simultâneas ao LLM")
    entrada_legado: str | None = Field(None, description="CSV explodido da v1 (aceite fase 2)")
    retry_erros: bool = Field(
        False, description="Reprocessa só as chaves de erros/3_erros.csv")
    reasoning: bool = Field(
        False, description="Mantém o raciocínio do modelo LIGADO. Padrão: desligado.")
    fonte: Literal["banco", "csv"] = Field(
        "banco", description="De onde vêm os itens e para onde vai a classificação")


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


def agrupar_por_texto(pendentes: list) -> list[dict]:
    """DEDUP: a descrição do PNCP é canônica e se repete MUITO (o mesmo texto reaparece
    milhares de vezes — às vezes centenas dentro de um único contrato). Classificamos cada
    texto ÚNICO uma vez e ESPALHAMOS o rótulo para todos os item_keys daquele texto.
    A saída continua por item_key (referência à ata/contrato intacta); só as chamadas de LLM
    caem de O(itens) para O(textos distintos). Chave = (descrição, unidade) — os dois campos
    que o classificador usa, então texto igual ⇒ mesma classe, sem perda.

    A chave é o `texto_hash` de `core.textos` — o MESMO que a ingestão grava em `item` e que
    `texto_classificacao` usa como PK. Duas normalizações diferentes aqui e lá fariam o dedup
    permanente errar e recomprar 320k classificações (docs/08_CONVENCOES.md §5.4)."""
    grupos: dict = {}
    for r in pendentes:
        chave = texto_hash(r["descricao_api"], r.get("unidade", ""))
        g = grupos.get(chave)
        if g is None:
            g = grupos[chave] = {"descricao_api": r["descricao_api"],
                                 "unidade": r.get("unidade", ""), "keys": []}
        g["keys"].append(r["item_key"])
    return list(grupos.values())


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Uma chamada por TEXTO único ainda não classificado — não por item."""
    if params.fonte == "banco":
        from pesquisa_precos.db.repos import classificacao as repo

        ok, detalhe = db.esta_disponivel()
        if not ok:
            return Estimativa(detalhes={"aviso": f"banco indisponível: {detalhe}"})
        with db.sessao() as s:
            n_textos, n_itens = repo.contar_pendentes(s)
            ja = len(repo.hashes_ja_classificados(s))
        n = n_textos if not params.limite else min(n_textos, params.limite)
        nome_provedor = ctx.provedores.resolucao("chat", provedor=params.provedor).info.nome
        preco = custo_por_chamada(ctx.config, nome_provedor)
        return Estimativa(
            unidades=n, chamadas_llm=n,
            custo_usd=None if preco is None else n * preco,
            duracao_s=n / max(params.concurrency, 1) * 2,
            detalhes={"fonte": "banco", "itens_pendentes": n_itens,
                      "textos_unicos": n_textos,
                      "dedup": f"{n_itens / max(n_textos, 1):.1f}x",
                      "textos_ja_classificados (nunca repagos)": ja},
        )

    if not params.entrada_legado and not ENTRADA.exists():
        return Estimativa(detalhes={"aviso": f"{ENTRADA} ausente — rode a etapa 2 antes."})
    df = carregar_itens(params.entrada_legado)
    feitas = ler_chaves_concluidas(str(SAIDA), "item_key")
    pend = [r for _, r in df.iterrows() if r["item_key"] not in feitas]
    tarefas = agrupar_por_texto(pend)
    n = len(tarefas) if not params.limite else min(len(tarefas), params.limite)
    # `estimar()` nunca chama provedor pago (docs/03_ETAPAS.md §1.1 regra 5) — só lê qual
    # provedor ATENDERIA a chamada (banco → `.env`, `providers.resolver`) para achar o preço.
    nome_provedor = ctx.provedores.resolucao("chat", provedor=params.provedor).info.nome
    preco = custo_por_chamada(ctx.config, nome_provedor)
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
    # Fase 7 (ADR-006/ADR-014): banco (`capacidade_provedor`) manda se estiver configurado;
    # sem isso, cai no `.env` como sempre — e só nesse caso a validação legada de `.env` faz
    # sentido (config via banco já traz tudo que precisa, ou falha alto e claro na chamada).
    resolucao_chat = ctx.provedores.resolucao("chat", provedor=params.provedor)
    nome_provedor = resolucao_chat.info.nome
    if resolucao_chat.origem == "env":
        msg = exigir(cfg, nome_provedor)
        if msg:
            raise SystemExit(msg)

    if params.fonte == "banco":
        # Prompt e reasoning são resolvidos igual nos dois caminhos; só o IO muda.
        reasoning_kw = {}
        if not params.reasoning:
            reasoning_kw = ({"extra_body": {"reasoning_effort": "none"}}
                            if nome_provedor == "local" else {"reasoning": {"enabled": False}})
        try:
            with db.sessao() as sessao:
                prompts_ativos = prompts_resolver.carregar_ativos(sessao,
                                                                  ["classificar_item"])
        except Exception:  # noqa: BLE001 — sem banco de prompts, cai no hardcoded
            prompts_ativos = {}
        return executar_no_banco(params, ctx, resolucao_chat, prompts_ativos, reasoning_kw)

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
                        if nome_provedor == "local" else {"reasoning": {"enabled": False}})
    ctx.log("debug", f"[dim][3] provedor de chat: {nome_provedor} (origem: "
                     f"{resolucao_chat.origem}) · reasoning: "
                     f"{'ligado (default do modelo)' if params.reasoning else 'DESLIGADO'}[/]")
    # Prompt 'classificar_item' resolvido UMA vez, fora dos workers (Fase 6): compartilha um
    # dict imutável em vez de repassar `Session` para threads (não é thread-safe).
    try:
        with db.sessao() as sessao:
            prompts_ativos = prompts_resolver.carregar_ativos(sessao, ["classificar_item"])
    except Exception:  # noqa: BLE001 — sem banco configurado, cai no prompt hardcoded
        prompts_ativos = {}

    _tls = threading.local()

    def _curador():
        if not hasattr(_tls, "c"):
            _tls.c = ctx.provedores.novo_chat(
                provedor=params.provedor,
                curador_kwargs={"prompts_ativos": prompts_ativos, **reasoning_kw}).curador
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


# ── Caminho `--fonte banco` (Fase 10) ───────────────────────────────────────────────
#
# O dedup por texto — o que segura o custo desta etapa, a mais cara do ciclo — deixa de ser
# intra-execução e vira PERMANENTE (ADR-007): `texto_classificacao` sobrevive entre runs, e
# um texto já pago nunca mais volta ao modelo. No CSV, o agrupamento era refeito a cada
# execução sobre 1,6 milhão de linhas em memória; aqui o `texto_hash` já veio calculado da
# ingestão da etapa 2 e o agrupamento é do banco.

def executar_no_banco(params: Params, ctx: ContextoExecucao,
                      resolucao_chat, prompts_ativos: dict,
                      reasoning_kw: dict) -> ResultadoEtapa:
    from pesquisa_precos.db.repos import classificacao as repo

    ok_banco, detalhe = db.esta_disponivel()
    if not ok_banco:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env "
                         f"ou rode com --fonte csv.")

    with db.sessao() as s:
        n_textos, n_itens_pend = repo.contar_pendentes(s)
        tarefas = repo.textos_pendentes(s, params.limite)
    if not tarefas:
        with db.sessao() as s:
            n_recomputadas = repo.recomputar_item_categoria(s)
            s.commit()
        ctx.log("info", "[3] Nada a classificar (todo texto já está em texto_classificacao).")
        return ResultadoEtapa(metricas={"textos_ja_classificados": 0,
                                        "item_categoria_recomputadas": n_recomputadas})

    n_itens = sum(g["n_itens"] for g in tarefas)
    limite_txt = f" — rodando só {len(tarefas)} (limite)" if params.limite else ""
    ctx.log("info", f"[bold][3] Dedup: {n_itens_pend} itens → {n_textos} textos únicos[/]"
                    f"{limite_txt} · classificando {len(tarefas)} textos "
                    f"({n_itens} itens), concorrência: {params.concurrency}")

    _tls = threading.local()

    def _curador():
        if not hasattr(_tls, "c"):
            _tls.c = ctx.provedores.novo_chat(
                provedor=params.provedor,
                curador_kwargs={"prompts_ativos": prompts_ativos, **reasoning_kw}).curador
        return _tls.c

    nome_provedor = resolucao_chat.info.nome
    modelo = getattr(resolucao_chat.info, "modelo", None) or nome_provedor
    n_erros, n_ok = [0], [0]
    lote: list[tuple] = []

    def descarregar():
        """Grava o lote acumulado. Em lote e não por texto: `COPY` numa transação por item
        seria mais lento que a própria chamada de LLM que estamos economizando."""
        if not lote:
            return
        with db.conexao_bruta() as conn:
            repo.gravar(conn, lote)
            conn.commit()
        lote.clear()

    def fn(g):
        return _curador().classificar_categoria(g["descricao"], g.get("unidade") or "")

    def ok(g, res):
        conf = res.get("confianca", "")
        if conf == "erro":
            n_erros[0] += 1
            ctx.erro_item(g["texto_hash"], res.get("_erro"), nome=g["descricao"])
            return   # texto com erro NÃO entra na tabela: entrar marcaria como pago algo
                     # que não foi classificado, e o retry nunca mais o encontraria.
        n_ok[0] += 1
        # `confianca` é `real` no banco e PALAVRA no LLM — a escala ordinal é declarada em
        # `repo.CONFIANCA_ORDINAL`, a mesma que a migração usa.
        lote.append((g["texto_hash"], g["descricao"], g.get("unidade"),
                     res["categorias"], repo.confianca_para_real(conf),
                     res.get("_prompt_versao_id"), modelo, nome_provedor, None))
        if len(lote) >= 500:
            descarregar()

    def err(g, exc):
        n_erros[0] += 1
        ctx.erro_item(g["texto_hash"], exc, nome=g["descricao"])

    ctx.progresso(0, len(tarefas), descricao="classificando")
    try:
        executar_paralelo(tarefas, fn, concurrency=params.concurrency,
                          on_result=ok, on_error=err,
                          on_progress=lambda f, t: ctx.progresso(f, t))
    finally:
        descarregar()   # o que já foi pago é gravado mesmo se a etapa cair no meio

    with db.sessao() as s:
        n_recomputadas = repo.recomputar_item_categoria(s)
        s.commit()
        contagens = repo.contar(s)

    cor = "yellow" if n_erros[0] else "green"
    ctx.log("info", f"[bold {cor}][3] Concluído.[/] {n_erros[0]} erros. "
                    f"→ texto_classificacao ({contagens.get('texto_classificacao', 0)} textos), "
                    f"item_categoria (+{n_recomputadas})")

    return ResultadoEtapa(
        processados=n_ok[0], erros=n_erros[0],
        metricas={"textos_unicos": len(tarefas), "itens_afetados": n_itens,
                  "item_categoria_recomputadas": n_recomputadas,
                  "dedup": f"{n_itens_pend / max(n_textos, 1):.1f}x"},
        preview=[{"descricao": g["descricao"][:200], "itens": g["n_itens"]}
                 for g in tarefas[:30]],
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
