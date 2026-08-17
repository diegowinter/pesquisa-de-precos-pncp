"""
Etapa 2 — Coleta larga no PNCP (busca por termo → filtro homologado → download → explode).

A lógica de negócio vive em `core/coleta/coleta_pncp.py` (funções puras). Aqui fica a
orquestração resumível e o dedup de documento muitos-para-muitos.

Para cada termo de cada conceito (data/1_conceitos_termos.csv) e cada tipo de documento
(contrato, ata):
  - busca paginada no PNCP;
  - dedup por numeroControlePNCP: documento já visto NÃO é reprocessado — apenas o conceito
    atual é acrescentado a conceitos_origem (via data/checkpoints/2_conceitos_extra.csv);
  - documento novo: baixa PDFs, consulta itens homologados e explode 1 linha por item.

Entradas: data/1_conceitos_termos.csv.
Saída: data/2_itens_coletados.csv (append-only). Consolidação (merge dos conceitos extras)
é feita por coleta_pncp.carregar_itens_coletados() — todas as etapas seguintes leem por ela.
Chave de resumo: (termo, tipo_doc) em data/checkpoints/2_progresso.csv (granularidade de
termo/fonte; o dedup por documento cobre a sobreposição entre termos). Erros: erros/2_erros.csv.

Fase 8 (ADR-011): esta etapa NÃO baixa mais PDF — só a "capa" (metadados + itens homologados
da API). O download passa para a etapa 5, depois do corte (etapa 4), preservando os
identificadores (`numero_sequencial`/`numero_sequencial_ata`) e a `url_pncp` (ADR-012) que a
etapa 5 precisa para refazer a listagem de arquivos sem reconsultar a busca.

Rodada de atualização (--atualizar): revisita TODOS os (termo, fonte), mas para de paginar ao
cruzar o watermark — a maior data_atualizacao_pncp já vista naquela busca
(data/checkpoints/2_watermark.csv). A busca do PNCP vem ordenada por data_atualizacao_pncp desc
(validado; a data de publicação NÃO vem ordenada), então coletar só o novo é seguro. O watermark
é alimentado em qualquer rodada; a primeira atualização sobre um acervo semeado do v2 (sem marca)
faz uma varredura completa e semeia o watermark — as seguintes ficam baratas.

NÃO fazer: reprocessar documento já visto (o dedup é o que segura o custo das etapas de baixo)
nem semear o watermark de novo — isso já foi feito uma vez, conservadoramente (ver CLAUDE.md).

Uso: python -m pesquisa_precos.etapas.e2_coletar [--conceitos c1,c2] [--ignorar-cache]
                                                 [--atualizar] [--limite-termos N]
"""

import json
import os
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
from pesquisa_precos.core.coleta.coleta_pncp import COLUNAS_ITENS, FONTES
from pesquisa_precos.core.io_seguro import EscritorSeguro, ler_chaves_concluidas
from pesquisa_precos.etapas.base import (
    ContextoExecucao,
    Estimativa,
    ResultadoEtapa,
    subprogresso,
)

CHAVE = "2"
VERSAO_CODIGO = "2.0.0"  # Fase 8/ADR-011: parou de baixar PDF (ver docstring do módulo)

CONCEITOS = paths.E1_TERMOS
SAIDA = paths.E2_ITENS
ARQUIVOS_DIR = paths.ARQUIVOS
CK_PROGRESSO = paths.CK_2_PROGRESSO
CK_CONCEITOS_EXTRA = paths.CK_2_CONCEITOS_EXTRA
CK_WATERMARK = paths.CK_2_WATERMARK
CK_PENDENTES = paths.CK_2_PENDENTES
ERROS = paths.ERROS_2


class Params(BaseModel):
    conceitos: str | None = Field(None, description="Filtra conceitos (vírgula-separados)")
    ignorar_cache: bool = Field(False, description="Reprocessa termos já concluídos")
    atualizar: bool = Field(
        False, description="Rodada de atualização: revisita TODOS os termos, mas para de "
                           "paginar ao cruzar o watermark. Coleta só o novo.")
    limite_termos: int | None = Field(None, description="Máx. de termos por conceito (teste)")
    tam_pagina: int | None = Field(None, description="Tamanho da página da busca do PNCP")


def carregar_watermark() -> dict[tuple[str, str], str]:
    """(termo, tipo_doc) → maior data_atualizacao_pncp já vista naquela busca.

    O PNCP ordena a busca por `data_atualizacao_pncp` desc (validado com dados reais; a data
    de *publicação* NÃO vem ordenada). Na rodada de atualização usamos essa marca para parar
    de paginar assim que os resultados ficam mais antigos que a última visita.
    """
    if not (CK_WATERMARK.exists() and CK_WATERMARK.stat().st_size > 0):
        return {}
    df = pd.read_csv(CK_WATERMARK, dtype=str, encoding="utf-8").fillna("")
    return {(r["termo"], r["tipo_doc"]): r["data_max"] for _, r in df.iterrows()}


def salvar_watermark(wm: dict[tuple[str, str], str]) -> None:
    """Grava o mapa de watermark de forma atômica (upsert de todas as chaves)."""
    CK_WATERMARK.parent.mkdir(parents=True, exist_ok=True)
    linhas = [{"termo": t, "tipo_doc": f, "data_max": d} for (t, f), d in wm.items()]
    tmp = CK_WATERMARK.with_suffix(".csv.tmp")
    pd.DataFrame(linhas, columns=["termo", "tipo_doc", "data_max"]).to_csv(
        tmp, index=False, encoding="utf-8")
    os.replace(tmp, CK_WATERMARK)


# ── Pendentes (documentos 'sem_homologado', revisitáveis numa rodada futura) ──────
COLS_PENDENTES = ["numeroControlePNCP", "tipo_doc", "conceito", "motivo", "data", "base_json"]


def carregar_pendentes() -> dict[str, dict]:
    """numeroControlePNCP → registro do pendente (com o `base` desserializado para revisita)."""
    if not (CK_PENDENTES.exists() and CK_PENDENTES.stat().st_size > 0):
        return {}
    df = pd.read_csv(CK_PENDENTES, dtype=str, encoding="utf-8").fillna("")
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        try:
            base = json.loads(r["base_json"]) if r["base_json"] else {}
        except (ValueError, TypeError):
            continue
        out[r["numeroControlePNCP"]] = {
            "tipo_doc": r["tipo_doc"], "conceito": r["conceito"],
            "motivo": r["motivo"], "data": r["data"], "base": base,
        }
    return out


def salvar_pendentes(pend: dict[str, dict]) -> None:
    """Reescreve o CSV de pendentes de forma atômica (o conjunto atual, sem os resolvidos)."""
    CK_PENDENTES.parent.mkdir(parents=True, exist_ok=True)
    linhas = [{
        "numeroControlePNCP": ctrl, "tipo_doc": rec["tipo_doc"], "conceito": rec["conceito"],
        "motivo": rec["motivo"], "data": rec["data"],
        "base_json": json.dumps(rec["base"], ensure_ascii=False),
    } for ctrl, rec in pend.items()]
    tmp = CK_PENDENTES.with_suffix(".csv.tmp")
    pd.DataFrame(linhas, columns=COLS_PENDENTES).to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, CK_PENDENTES)


def carregar_conceitos(filtro: set[str] | None) -> list[dict]:
    if not CONCEITOS.exists():
        raise SystemExit(f"{CONCEITOS} ausente. Rode a etapa 1 antes.")
    df = pd.read_csv(CONCEITOS, dtype=str, encoding="utf-8").fillna("")
    linhas = df.to_dict("records")
    if filtro:
        linhas = [l for l in linhas if l["conceito"] in filtro]
    return linhas


def indexar_docs_existentes() -> dict[str, list[str]]:
    """Reconstrói numeroControlePNCP → [item_key] do CSV principal (para dedup no resume)."""
    doc_para_itens: dict[str, list[str]] = {}
    if not (SAIDA.exists() and SAIDA.stat().st_size > 0):
        return doc_para_itens
    df = pd.read_csv(SAIDA, dtype=str, encoding="utf-8").fillna("")
    for _, r in df.iterrows():
        doc_para_itens.setdefault(r["numeroControlePNCP"], []).append(r["item_key"])
    return doc_para_itens


def _montar_tarefas(params: Params) -> tuple[list[tuple[str, str, str]], set]:
    """(termo, fonte, conceito) a fazer + as chaves já concluídas."""
    filtro = set(c.strip() for c in params.conceitos.split(",")) if params.conceitos else None
    conceitos = carregar_conceitos(filtro)
    # No modo atualizar revisitamos TODOS os (termo, fonte) — a parada por watermark é que evita
    # o retrabalho — logo não usamos o progresso como skip-list. Fora dele, mantém o resume normal.
    if params.ignorar_cache or params.atualizar:
        feitas: set = set()
    else:
        feitas = ler_chaves_concluidas(str(CK_PROGRESSO), ("termo", "tipo_doc"))
    tarefas = []
    for c in conceitos:
        termos = [t for t in c.get("termos", "").split("|") if t]
        if params.limite_termos:
            termos = termos[: params.limite_termos]
        for termo in termos:
            for fonte in FONTES:
                tarefas.append((termo, fonte, c["conceito"]))
    return tarefas, feitas


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Quantas buscas (termo × fonte) faltam. Só HTTP: não gasta LLM."""
    if not CONCEITOS.exists():
        return Estimativa(detalhes={"aviso": f"{CONCEITOS} ausente — rode a etapa 1 antes."})
    tarefas, feitas = _montar_tarefas(params)
    pendentes = [t for t in tarefas if (t[0], t[1]) not in feitas]
    return Estimativa(
        unidades=len(pendentes), chamadas_llm=0,
        detalhes={
            "buscas_totais": len(tarefas), "já_feitas": len(tarefas) - len(pendentes),
            "modo": "atualização (para no watermark)" if params.atualizar else "completa",
            "pendentes_sem_homologado": len(carregar_pendentes()) if params.atualizar else 0,
        },
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    os.makedirs(ARQUIVOS_DIR, exist_ok=True)
    os.makedirs(CK_PROGRESSO.parent, exist_ok=True)

    tarefas, feitas = _montar_tarefas(params)
    modo_atualizar = params.atualizar
    doc_para_itens = {} if params.ignorar_cache else indexar_docs_existentes()
    # Watermark é alimentado em qualquer rodada (semeia a marca já na coleta inicial); a parada
    # antecipada por ele só age no modo --atualizar.
    watermark = carregar_watermark()
    pendentes_docs = {} if params.ignorar_cache else carregar_pendentes()

    esc_itens = EscritorSeguro(str(SAIDA), COLUNAS_ITENS)
    esc_extra = EscritorSeguro(str(CK_CONCEITOS_EXTRA), ["item_key", "conceito"])
    esc_prog = EscritorSeguro(str(CK_PROGRESSO), ["termo", "tipo_doc"])

    pendentes = [t for t in tarefas if (t[0], t[1]) not in feitas]
    _modo = "[yellow]atualização (para no watermark)[/]" if modo_atualizar else "completa"
    ctx.log("info", f"[bold][2] Coleta no PNCP ({_modo}):[/] {len(pendentes)} buscas "
                    f"(termo×fonte) a fazer (já feitas: {len(tarefas) - len(pendentes)}, "
                    f"fontes: {', '.join(FONTES)})")

    total_itens = total_docs = total_erros = resolvidos = 0
    tam_pagina_kw = {"tam_pagina": params.tam_pagina} if params.tam_pagina else {}
    try:
        # Revisita de pendentes (só no --atualizar): re-consulta direto os documentos antes
        # 'sem_homologado'; se a homologação já saiu, viram itens coletados e saem da lista.
        if modo_atualizar and pendentes_docs:
            ctx.log("info", f"[2] Revisitando [bold]{len(pendentes_docs)}[/] pendentes "
                            f"(sem_homologado)…")
            feitos_rev = 0
            ctx.progresso(0, len(pendentes_docs),
                          descricao="pendentes · [green]0 resolvidos[/]")
            for ctrl, rec in list(pendentes_docs.items()):
                if ctx.cancelado():
                    break
                if doc_para_itens.get(ctrl):  # já coletado por outra via nesta rodada
                    del pendentes_docs[ctrl]
                    feitos_rev += 1
                    ctx.progresso(feitos_rev)
                    continue
                try:
                    linhas, status = coleta_pncp.revisitar_pendente(
                        rec["base"], rec["tipo_doc"], rec["conceito"])
                except Exception as exc:  # noqa: BLE001
                    ctx.erro_item(ctrl, exc, tipo=rec["tipo_doc"], nome="revisita")
                    feitos_rev += 1
                    ctx.progresso(feitos_rev)
                    continue
                if status == "ok":
                    for linha in linhas:
                        esc_itens.escrever(linha)
                    doc_para_itens[ctrl] = [l["item_key"] for l in linhas]
                    total_itens += len(linhas)
                    resolvidos += 1
                    del pendentes_docs[ctrl]
                feitos_rev += 1
                ctx.progresso(feitos_rev,
                              descricao=f"pendentes · [green]{resolvidos} resolvidos[/]")
            ctx.log("info", f"[2] Pendentes resolvidos: [green]{resolvidos}[/]; "
                            f"ainda pendentes: {len(pendentes_docs)}")

        # barra principal: buscas (termo×fonte); barra secundária: documentos da busca atual
        feitas_buscas = 0
        ctx.progresso(0, len(pendentes), descricao="buscas · [green]0 itens[/]")
        for termo, fonte, conceito in pendentes:
            if ctx.cancelado():
                break
            subprogresso(ctx, processados=0, total=None,
                         descricao=f"[cyan]{termo[:24]}[/] ({fonte})")
            n_docs_busca = 0
            wm = watermark.get((termo, fonte))  # maior data_atualizacao_pncp da última visita
            max_atu = wm or ""
            try:
                for r in coleta_pncp.iter_resultados(
                        termo, fonte,
                        on_total=lambda n: subprogresso(ctx, total=n),
                        **tam_pagina_kw):
                    n_docs_busca += 1  # 1 contrato/ata analisado
                    subprogresso(ctx, processados=n_docs_busca)
                    atu = r.get("data_atualizacao_pncp") or ""
                    if atu > max_atu:
                        max_atu = atu
                    # Parada antecipada (só no --atualizar): a busca vem por data_atualizacao
                    # desc; ao cruzar o watermark, tudo daqui pra frente já foi visto → para.
                    if modo_atualizar and wm and atu and atu < wm:
                        break
                    ctrl = r.get("numero_controle_pncp")
                    if not ctrl:
                        continue
                    if ctrl in doc_para_itens:
                        # documento já coletado: só acrescenta o conceito atual
                        for ik in doc_para_itens[ctrl]:
                            esc_extra.escrever({"item_key": ik, "conceito": conceito})
                        continue
                    linhas, status = coleta_pncp.coletar_documento(r, fonte, conceito)
                    if status != "ok":
                        if status == "erro":
                            total_erros += 1
                            ctx.erro_item(ctrl, status, tipo=fonte, nome=termo)
                        elif status == "sem_homologado":
                            # homologação pode sair depois → guarda p/ revisitar numa rodada futura
                            base = coleta_pncp.identificar(r, fonte)
                            pendentes_docs[ctrl] = {"tipo_doc": fonte, "conceito": conceito,
                                                    "motivo": status, "data": base.get("data", ""),
                                                    "base": base}
                        doc_para_itens[ctrl] = []  # marca visto (não reprocessa)
                        continue
                    for linha in linhas:
                        esc_itens.escrever(linha)
                    doc_para_itens[ctrl] = [l["item_key"] for l in linhas]
                    total_docs += 1
                    total_itens += len(linhas)
                    ctx.progresso(feitas_buscas,
                                  descricao=f"buscas · [green]{total_itens} itens[/]")
                esc_prog.escrever({"termo": termo, "tipo_doc": fonte})
                if max_atu:
                    watermark[(termo, fonte)] = max_atu
            except Exception as exc:  # noqa: BLE001
                total_erros += 1
                ctx.log("erro", f"[red]erro[/] {termo} ({fonte}): {str(exc)[:80]}")
                ctx.erro_item(termo, exc, tipo=fonte, nome=conceito)
            finally:
                feitas_buscas += 1
                ctx.progresso(feitas_buscas)
    finally:
        esc_itens.fechar()
        esc_extra.fechar()
        esc_prog.fechar()
        salvar_watermark(watermark)       # persiste a marca (mesmo em caso de queda no meio)
        salvar_pendentes(pendentes_docs)  # persiste os pendentes remanescentes
    cor = "yellow" if total_erros else "green"
    ctx.log("info", f"[bold {cor}][2] Concluído.[/] {total_docs} documentos novos, "
                    f"[bold]{total_itens}[/] itens coletados, {total_erros} erros"
                    f"{f', {resolvidos} pendentes resolvidos' if resolvidos else ''}. → {SAIDA}")

    return ResultadoEtapa(
        processados=total_itens, erros=total_erros,
        metricas={"documentos_novos": total_docs, "itens_coletados": total_itens,
                  "pendentes_resolvidos": resolvidos,
                  "pendentes_restantes": len(pendentes_docs)},
        preview=[],
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
