"""
Etapa 2 — Coleta larga no PNCP (busca por termo → filtro homologado → download → explode).

Sucessor de `1_obter_itens*.py` da v1; a lógica de negócio vive em `scripts/coleta_pncp.py`
(funções puras). Aqui fica a orquestração resumível e o dedup de documento muitos-para-muitos.

Para cada termo de cada conceito (data/1_conceitos_termos.csv) e cada tipo de documento
(contrato, ata):
  - busca paginada no PNCP;
  - dedup por numeroControlePNCP: documento já visto NÃO é reprocessado — apenas o conceito
    atual é acrescentado a conceitos_origem (via data/checkpoints/2_conceitos_extra.csv);
  - documento novo: baixa PDFs, consulta itens homologados e explode 1 linha por item.

Saída: data/2_itens_coletados.csv (append-only). Consolidação (merge dos conceitos extras)
é feita por coleta_pncp.carregar_itens_coletados() — todas as etapas seguintes leem por ela.

Chave de resumo: (termo, tipo_doc) em data/checkpoints/2_progresso.csv (granularidade de
termo/fonte; o dedup por documento cobre a sobreposição entre termos).

Rodada de atualização (--atualizar): revisita TODOS os (termo, fonte), mas para de paginar ao
cruzar o watermark — a maior data_atualizacao_pncp já vista naquela busca
(data/checkpoints/2_watermark.csv). A busca do PNCP vem ordenada por data_atualizacao_pncp desc
(validado; a data de publicação NÃO vem ordenada), então coletar só o novo é seguro. O watermark
é alimentado em qualquer rodada; a primeira atualização sobre um acervo semeado do v2 (sem marca)
faz uma varredura completa e semeia o watermark — as seguintes ficam baratas.

Uso: python -m pesquisa_precos.etapas.e2_coletar [--conceitos c1,c2] [--ignorar-cache]
                                                 [--atualizar] [--limite-termos N]
"""

import argparse
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from pesquisa_precos.config import paths
from pesquisa_precos.core import erros_log
from pesquisa_precos.core.coleta import coleta_pncp
from pesquisa_precos.core.coleta.coleta_pncp import COLUNAS_ITENS, FONTES
from pesquisa_precos.core.io_seguro import EscritorSeguro, ler_chaves_concluidas

console = Console()

CONCEITOS = paths.E1_TERMOS
SAIDA = paths.E2_ITENS
ARQUIVOS_DIR = paths.ARQUIVOS
CK_PROGRESSO = paths.CK_2_PROGRESSO
CK_CONCEITOS_EXTRA = paths.CK_2_CONCEITOS_EXTRA
CK_WATERMARK = paths.CK_2_WATERMARK
CK_PENDENTES = paths.CK_2_PENDENTES
ERROS = paths.ERROS_2


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


def main():
    ap = argparse.ArgumentParser(description="Etapa 2 — coleta larga no PNCP")
    ap.add_argument("--conceitos", help="Filtra conceitos (vírgula-separados)")
    ap.add_argument("--ignorar-cache", action="store_true", help="Reprocessa termos já concluídos")
    ap.add_argument("--atualizar", action="store_true",
                    help="Rodada de atualização: revisita TODOS os termos, mas para de paginar ao "
                         "cruzar o watermark (data_atualizacao_pncp da última visita). Coleta só o novo.")
    ap.add_argument("--limite-termos", type=int, default=None, help="Máx. de termos por conceito (teste)")
    ap.add_argument("--tam-pagina", type=int, default=None)
    args = ap.parse_args()

    os.makedirs(ARQUIVOS_DIR, exist_ok=True)
    os.makedirs(CK_PROGRESSO.parent, exist_ok=True)
    filtro = set(c.strip() for c in args.conceitos.split(",")) if args.conceitos else None
    conceitos = carregar_conceitos(filtro)

    modo_atualizar = args.atualizar
    # No modo atualizar revisitamos TODOS os (termo, fonte) — a parada por watermark é que evita
    # o retrabalho — logo não usamos o progresso como skip-list. Fora dele, mantém o resume normal.
    if args.ignorar_cache or modo_atualizar:
        feitas = set()
    else:
        feitas = ler_chaves_concluidas(str(CK_PROGRESSO), ("termo", "tipo_doc"))
    doc_para_itens = {} if args.ignorar_cache else indexar_docs_existentes()
    # Watermark é alimentado em qualquer rodada (semeia a marca já na coleta inicial); a parada
    # antecipada por ele só age no modo --atualizar.
    watermark = carregar_watermark()
    pendentes_docs = {} if args.ignorar_cache else carregar_pendentes()

    esc_itens = EscritorSeguro(str(SAIDA), COLUNAS_ITENS)
    esc_extra = EscritorSeguro(str(CK_CONCEITOS_EXTRA), ["item_key", "conceito"])
    esc_prog = EscritorSeguro(str(CK_PROGRESSO), ["termo", "tipo_doc"])

    # monta a lista de tarefas (termo, fonte, conceito) já respeitando --limite-termos
    tarefas = []
    for c in conceitos:
        termos = [t for t in c.get("termos", "").split("|") if t]
        if args.limite_termos:
            termos = termos[: args.limite_termos]
        for termo in termos:
            for fonte in FONTES:
                tarefas.append((termo, fonte, c["conceito"]))
    pendentes = [t for t in tarefas if (t[0], t[1]) not in feitas]
    _modo = "[yellow]atualização (para no watermark)[/]" if modo_atualizar else "completa"
    console.print(f"[bold][2] Coleta no PNCP ({_modo}):[/] {len(pendentes)} buscas (termo×fonte) a fazer "
                  f"(já feitas: {len(tarefas) - len(pendentes)}, fontes: {', '.join(FONTES)})")

    total_itens = total_docs = total_erros = resolvidos = 0
    tam_pagina_kw = {"tam_pagina": args.tam_pagina} if args.tam_pagina else {}
    progress = Progress(
        SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=28),
        MofNCompleteColumn(), TimeElapsedColumn(), console=console,
    )
    try:
        # Revisita de pendentes (só no --atualizar): re-consulta direto os documentos antes
        # 'sem_homologado'; se a homologação já saiu, viram itens coletados e saem da lista.
        if modo_atualizar and pendentes_docs:
            console.print(f"[2] Revisitando [bold]{len(pendentes_docs)}[/] pendentes (sem_homologado)…")
            with Progress(
                SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=28),
                MofNCompleteColumn(), TimeElapsedColumn(), console=console,
            ) as prog_rev:
                tarefa_rev = prog_rev.add_task("pendentes · [green]0 resolvidos[/]",
                                               total=len(pendentes_docs))
                for ctrl, rec in list(pendentes_docs.items()):
                    if doc_para_itens.get(ctrl):  # já coletado por outra via nesta rodada
                        del pendentes_docs[ctrl]
                        prog_rev.advance(tarefa_rev)
                        continue
                    try:
                        linhas, status = coleta_pncp.revisitar_pendente(
                            rec["base"], rec["tipo_doc"], str(ARQUIVOS_DIR), rec["conceito"])
                    except Exception as exc:  # noqa: BLE001
                        erros_log.logar_erro(str(ERROS), "2", rec["tipo_doc"], ctrl, "revisita", exc)
                        prog_rev.advance(tarefa_rev)
                        continue
                    if status == "ok":
                        for linha in linhas:
                            esc_itens.escrever(linha)
                        doc_para_itens[ctrl] = [l["item_key"] for l in linhas]
                        total_itens += len(linhas)
                        resolvidos += 1
                        del pendentes_docs[ctrl]
                        prog_rev.update(tarefa_rev,
                                        description=f"pendentes · [green]{resolvidos} resolvidos[/]")
                    prog_rev.advance(tarefa_rev)
            console.print(f"[2] Pendentes resolvidos: [green]{resolvidos}[/]; "
                          f"ainda pendentes: {len(pendentes_docs)}")

        with progress:
            # barra 1: buscas (termo×fonte); barra 2: documentos da busca atual
            tarefa = progress.add_task(f"buscas · [green]0 itens[/]", total=len(pendentes))
            docs = progress.add_task("documentos", total=None)
            for termo, fonte, conceito in pendentes:
                progress.reset(docs, total=None,
                               description=f"[cyan]{termo[:24]}[/] ({fonte})")
                wm = watermark.get((termo, fonte))  # maior data_atualizacao_pncp da última visita
                max_atu = wm or ""
                try:
                    for r in coleta_pncp.iter_resultados(
                            termo, fonte, on_total=lambda n: progress.update(docs, total=n),
                            **tam_pagina_kw):
                        progress.advance(docs)  # 1 contrato/ata analisado
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
                        linhas, status = coleta_pncp.coletar_documento(
                            r, fonte, str(ARQUIVOS_DIR), conceito)
                        if status != "ok":
                            if status == "erro":
                                total_erros += 1
                                erros_log.logar_erro(str(ERROS), "2", fonte, ctrl, termo, status)
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
                        progress.update(tarefa, description=f"buscas · [green]{total_itens} itens[/]")
                    esc_prog.escrever({"termo": termo, "tipo_doc": fonte})
                    if max_atu:
                        watermark[(termo, fonte)] = max_atu
                except Exception as exc:  # noqa: BLE001
                    total_erros += 1
                    console.log(f"[red]erro[/] {termo} ({fonte}): {str(exc)[:80]}")
                    erros_log.logar_erro(str(ERROS), "2", fonte, termo, conceito, exc)
                finally:
                    progress.advance(tarefa)
    finally:
        esc_itens.fechar()
        esc_extra.fechar()
        esc_prog.fechar()
        salvar_watermark(watermark)      # persiste a marca (mesmo em caso de queda no meio)
        salvar_pendentes(pendentes_docs)  # persiste os pendentes remanescentes
    cor = "yellow" if total_erros else "green"
    console.print(f"[bold {cor}][2] Concluído.[/] {total_docs} documentos novos, "
                  f"[bold]{total_itens}[/] itens coletados, {total_erros} erros"
                  f"{f', {resolvidos} pendentes resolvidos' if resolvidos else ''}. → {SAIDA}")


if __name__ == "__main__":
    main()
