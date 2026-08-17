"""
Etapa 5 — Download + extração + enriquecimento, com estratégias plugáveis (Fase 8, ADR-010).

Substitui o par `e5a_ocr.py` (parse/OCR) + `e5b_extrair.py` (janela) e o embrião
`e5_alt_a_tabela.py`/`e5_alt_b_casar.py` (tabela por visão). Fluxo por documento
(docs/03_ETAPAS.md §5, docs/04_FASES.md Fase 8):

    baixa PDF → extrai texto (nativo + OCR nas páginas escaneadas)
              → grava data/5_pdf_texto.csv
              → DESCARTA o PDF (ADR-012: url_pncp + hash preservados, texto é o ativo)
              → aplica a estratégia (janela | completa | visao | auto)
              → grava item_enriquecido (contrato único, independente de estratégia)

Mudança em relação à v3 anterior à Fase 8: a etapa 2 não baixa mais PDF (ADR-011) — o
download acontece AQUI, depois do corte da etapa 4, só para os documentos que sobreviveram.
Isso evita baixar (e descartar) PDF de documento que nunca vira item confirmado.

Entradas: data/4_itens_sobreviventes.csv (numeroControlePNCP + numero_sequencial[_ata] +
tipo_doc + orgao_cnpj + ano, herdados da etapa 2 — usados para refazer
`consultar_arquivos.listar_arquivos()` sem reconsultar a busca).
Saídas: data/5_pdf_texto.csv (texto por página, append, resumível por numeroControlePNCP),
        data/5_itens_enriquecidos.csv (contrato de saída — item_key, descricao_final,
        fonte_descricao, preco_api, preco_pdf, divergencia_preco, fornecedor,
        quantidade_pdf, status, destino, estrategia, doc_status),
        data/5_itens_destino.csv (projeção item_key→destino, o que a 6a consome),
        data/5_documento_extracao.csv (1 linha por (documento, estratégia) — custo/páginas).
Chave de resumo: numeroControlePNCP (documento) — reprocessar um documento sobrescreve o
veredito de TODOS os seus itens (última linha vence na leitura, ver `core.io_seguro`); é o
mecanismo por trás de "reprocessar este documento com outra estratégia".

NÃO fazer: persistir o PDF além da vida do worker (sempre `try/finally` + `shutil.rmtree`);
usar o preço como critério de aceite (é SAÍDA, não filtro — docs/08_CONVENCOES.md §5.9);
truncar documento grande em silêncio na estratégia `completa` (usar `estrategias.completa.
dividir_em_chunks`, que tem overlap).

Uso: python -m pesquisa_precos.etapas.e5_extrair [--estrategia auto|janela|completa|visao]
     [--provedor openrouter|local] [--concurrency-docs 4] [--concurrency-llm 8]
     [--documentos <numeroControlePNCP,...>] [--limite-docs N]
"""

import os
import shutil
import sys
import threading
from collections import defaultdict

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from pydantic import BaseModel, Field
from typing import Literal

from pesquisa_precos.config import paths
from pesquisa_precos.config.settings import exigir
from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.core.coleta import consultar_arquivos
from pesquisa_precos.core.io_seguro import EscritorSeguro, ler_chaves_concluidas, ler_csv, escrever_csv
from pesquisa_precos.core.paralelo import executar_paralelo
from pesquisa_precos.db import sessao as db
from pesquisa_precos.estrategias import base as estr_base
from pesquisa_precos.estrategias import completa as estr_completa
from pesquisa_precos.estrategias import janela as estr_janela
from pesquisa_precos.estrategias import roteamento
from pesquisa_precos.estrategias import visao as estr_visao
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa
from pesquisa_precos.providers import ocr_pdf
from pesquisa_precos.providers.llm_curador import Curador

CHAVE = "5"
VERSAO_CODIGO = "1.0.0"

SOBREVIVENTES = paths.E4_SOBREVIVENTES
PAGINAS = paths.E5_PDF_TEXTO
SAIDA = paths.E5_ENRIQUECIDOS
DESTINO = paths.E5_DESTINO
DOC_EXTRACAO = paths.E5_DOC_EXTRACAO
STAGING = paths.ARQUIVOS

COLS_PAGINAS = ["numeroControlePNCP", "arquivo", "pagina", "fonte", "texto"]
COLS_ENRIQUECIDOS = ["item_key", "descricao_final", "fonte_descricao", "preco_api", "preco_pdf",
                     "divergencia_preco", "fornecedor", "quantidade_pdf", "status", "destino",
                     "estrategia", "doc_status"]
COLS_DESTINO = ["item_key", "destino", "doc_status"]
COLS_DOC_EXTRACAO = ["numeroControlePNCP", "estrategia", "n_paginas", "n_paginas_ocr",
                     "n_itens_tabela", "chamadas_llm", "doc_status"]

ESTRATEGIAS_VALIDAS = ("auto", "janela", "completa", "visao")


class Params(BaseModel):
    estrategia: Literal["auto", "janela", "completa", "visao"] = Field(
        "auto", description="Estratégia de extração; 'auto' roteia por documento (ADR-010)")
    provedor: str = Field("openrouter", description="Provedor de LLM [local|openrouter]")
    concurrency_docs: int = Field(
        4, ge=1, le=16, description="Documentos processados em paralelo (download+OCR+extração)")
    concurrency_llm: int = Field(
        8, ge=1, le=32, description="Chamadas de LLM em paralelo por item, dentro de um documento")
    janela_max: int = Field(9000, ge=1000, description="Teto de chars da janela (estratégia janela)")
    raio_preco: int = Field(1500, ge=100, description="Raio ao redor de cada ocorrência do preço")
    tamanho_tabela: int = Field(
        2500, ge=0, description="Estimativa de chars da tabela — usado na fórmula do roteamento auto")
    limiar_visao: int = Field(
        3, ge=1, description="Nº mínimo de itens sobreviventes p/ escalar a 'visao' quando o "
                             "documento fica suspeito/ilegível após janela/completa")
    max_paginas: int | None = Field(None, description="Teto de páginas por documento (OCR/visão)")
    pular_ocr: bool = Field(False, description="Só texto nativo (não chama o OCR)")
    limite_docs: int | None = Field(None, description="Teto de documentos (debug)")
    documentos: str | None = Field(
        None, description="numeroControlePNCP separados por vírgula — força reprocesso mesmo "
                          "já feito (usa --estrategia explícita para forçar a rota)")


def _documentos_alvo(params: Params) -> set[str]:
    if not params.documentos:
        return set()
    return {d.strip() for d in params.documentos.split(",") if d.strip()}


def _grupos_por_documento(sob: pd.DataFrame) -> dict[str, list[dict]]:
    grupos: dict[str, list[dict]] = defaultdict(list)
    for r in sob.to_dict("records"):
        grupos[r["numeroControlePNCP"]].append(r)
    return grupos


def _documentos_pendentes(params: Params) -> tuple[dict[str, list[dict]], list[str], set[str]]:
    sob = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    grupos = _grupos_por_documento(sob)
    feitos_itens = ler_chaves_concluidas(str(SAIDA), "item_key")
    forcados = _documentos_alvo(params)
    pend = [doc for doc, itens in grupos.items()
            if doc in forcados or not all(it["item_key"] in feitos_itens for it in itens)]
    if params.limite_docs:
        pend = pend[: params.limite_docs]
    return grupos, pend, feitos_itens


def _baixar_pdfs(item0: dict, doc_ctrl: str) -> tuple[str, list[str]]:
    """Baixa os PDFs do documento numa pasta de STAGING temporária (descartada pelo chamador
    logo após a extração — ADR-012: só o texto sobrevive). Devolve (pasta, nomes_arquivos)."""
    fonte = item0.get("tipo_doc", "")
    cnpj = item0.get("orgao_cnpj", "")
    ano = item0.get("ano", "")
    seq = item0.get("numero_sequencial", "")
    seq_ata = item0.get("numero_sequencial_ata") or None
    if not all([fonte, cnpj, ano, seq]):
        return "", []
    arquivos = consultar_arquivos.listar_arquivos(fonte, cnpj, ano, seq, seq_ata, silent=True)
    alvos = consultar_arquivos.selecionar_do_tipo(arquivos, fonte)
    if not alvos:
        return "", []
    pasta = os.path.join(str(STAGING), consultar_arquivos.sanitizar(doc_ctrl))
    nomes = consultar_arquivos.baixar_arquivos(alvos, pasta, silent=True)
    return pasta, nomes


def _parsear_e_ocr(pasta: str, nomes: list[str], doc_ctrl: str, pular_ocr: bool,
                   provedor_ocr) -> tuple[str, list[dict], int]:
    """Extrai texto nativo + OCR das páginas escaneadas. Devolve (texto_doc_concatenado,
    linhas_para_paginas.csv, n_paginas_ocr)."""
    textos_por_pagina: list[tuple[int, str]] = []
    linhas_csv: list[dict] = []
    n_ocr = 0
    for nome in nomes:
        caminho = os.path.join(pasta, nome)
        try:
            paginas = ocr_pdf.extrair_paginas(caminho)
        except Exception:  # noqa: BLE001
            continue
        for pg in paginas:
            fonte_txt, texto = "nativo", pg["texto"]
            if ocr_pdf.pagina_escaneada(pg["densidade"]) and not pular_ocr and provedor_ocr:
                try:
                    png = ocr_pdf.rasterizar(caminho, pg["_page_index"])
                    ocr_txt = provedor_ocr.ocr_pagina(png)
                    if ocr_txt:
                        fonte_txt, texto = "ocr", ocr_txt
                        n_ocr += 1
                except Exception:  # noqa: BLE001
                    pass
            textos_por_pagina.append((pg["pagina"], texto))
            linhas_csv.append({"numeroControlePNCP": doc_ctrl, "arquivo": nome,
                               "pagina": pg["pagina"], "fonte": fonte_txt, "texto": texto})
    texto_doc = "\n".join(t for _, t in sorted(textos_por_pagina))
    return texto_doc, linhas_csv, n_ocr


def _processar_estrategia(estrategia: str, curador_factory, texto_doc: str, pasta: str,
                          itens_doc: list[dict], params: Params) -> dict[str, dict]:
    """Devolve `item_key -> extraido` (shape de `estrategias.base.validar_extracao`).

    `janela` faz UMA chamada de LLM por item — para isso valer a pena, os itens do documento
    são extraídos em paralelo (`concurrency_llm`), e cada thread pega o SEU PRÓPRIO `Curador`
    via `curador_factory()` (thread-local): compartilhar um cliente entre threads serializa as
    chamadas (mesmo cuidado da antiga `e5b_extrair.py`). `completa`/`visao` fazem só 1–2
    chamadas "caras" por documento (tabela) + casamentos baratos; rodam sequenciais na thread
    do documento — o paralelismo de `concurrency_docs` já basta para elas.
    """
    if estrategia == "janela":
        def fn(item):
            return estr_janela.extrair_item(curador_factory(), texto_doc, item,
                                            janela_max=params.janela_max,
                                            raio_preco=params.raio_preco)
        out: dict[str, dict] = {}
        def ok(item, res):
            out[item["item_key"]] = res
        def err(item, _exc):
            out[item["item_key"]] = {"encontrado": False}
        executar_paralelo(itens_doc, fn, concurrency=params.concurrency_llm, on_result=ok, on_error=err)
        return out

    curador = curador_factory()
    if estrategia == "completa":
        tabela = estr_completa.extrair_tabela(curador, texto_doc)
        return estr_base.casar_itens_contra_tabela(curador, itens_doc, tabela)

    # visao
    tabela = estr_visao.extrair_tabela(curador, pasta, max_paginas=params.max_paginas)
    return estr_base.casar_itens_contra_tabela(curador, itens_doc, tabela)


def _linha_enriquecido(item: dict, extraido: dict, estrategia: str) -> dict:
    status, preco_pdf, div = estr_base.validar_extracao(extraido, item)
    achou_pdf = status.startswith("pdf_ok")
    return {
        "item_key": item["item_key"],
        "descricao_final": (extraido.get("descricao_completa") or "").strip() if achou_pdf
                           else item.get("descricao_api", ""),
        "fonte_descricao": "pdf" if achou_pdf else "api",
        "preco_api": item.get("preco_unitario", ""),
        "preco_pdf": "" if preco_pdf is None else preco_pdf,
        "divergencia_preco": "" if div is None else div,
        "fornecedor": (extraido.get("_fornecedor") or "") if achou_pdf else "",
        "quantidade_pdf": extraido.get("quantidade", "") if achou_pdf else "",
        "status": status,
        "estrategia": estrategia,
        # doc_status/destino preenchidos por quem chama, depois de conhecer TODOS os itens do doc
    }


def _processar_documento(doc_ctrl: str, itens_doc: list[dict], params: Params,
                         curador_factory, provedor_ocr) -> tuple[list[dict], list[dict]]:
    """Processa UM documento inteiro: baixa → parseia/OCR → descarta PDF → roteia estratégia →
    extrai → valida. Devolve (linhas_item_enriquecido_sem_doc_status, linhas_documento_extracao).
    `linhas_documento_extracao` pode ter 2 entradas quando há escalonamento para `visao`.
    """
    doc_extracao: list[dict] = []
    pasta = ""
    try:
        pasta, nomes = _baixar_pdfs(itens_doc[0], doc_ctrl)
        if not nomes:
            linhas = [{**_linha_enriquecido(it, {"encontrado": False}, "janela"),
                      "status": "sem_texto", "doc_status": "ilegivel", "destino": "revisar"}
                     for it in itens_doc]
            doc_extracao.append({"numeroControlePNCP": doc_ctrl, "estrategia": "janela",
                                 "n_paginas": 0, "n_paginas_ocr": 0, "n_itens_tabela": 0,
                                 "chamadas_llm": 0, "doc_status": "ilegivel"})
            return linhas, doc_extracao

        texto_doc, linhas_paginas, n_ocr = _parsear_e_ocr(
            pasta, nomes, doc_ctrl, params.pular_ocr, provedor_ocr)
        _grava_paginas(linhas_paginas)

        if not texto_doc.strip():
            linhas = [{**_linha_enriquecido(it, {"encontrado": False}, "janela"),
                      "status": "sem_texto", "doc_status": "ilegivel", "destino": "revisar"}
                     for it in itens_doc]
            doc_extracao.append({"numeroControlePNCP": doc_ctrl, "estrategia": "janela",
                                 "n_paginas": len(linhas_paginas), "n_paginas_ocr": n_ocr,
                                 "n_itens_tabela": 0, "chamadas_llm": 0, "doc_status": "ilegivel"})
            return linhas, doc_extracao

        estrategia = params.estrategia
        if estrategia == "auto":
            estrategia = roteamento.escolher_estrategia(
                n_itens=len(itens_doc), tamanho_texto_chars=len(texto_doc),
                janela_max=params.janela_max, tamanho_tabela=params.tamanho_tabela)

        extraidos = _processar_estrategia(estrategia, curador_factory, texto_doc, pasta, itens_doc, params)
        status_por_item = {ik: estr_base.validar_extracao(ex, next(
            it for it in itens_doc if it["item_key"] == ik))[0] for ik, ex in extraidos.items()}
        doc_status = estr_base.doc_status_de_motivos(status_por_item)
        doc_extracao.append({"numeroControlePNCP": doc_ctrl, "estrategia": estrategia,
                             "n_paginas": len(linhas_paginas), "n_paginas_ocr": n_ocr,
                             "n_itens_tabela": len(extraidos), "chamadas_llm": len(itens_doc),
                             "doc_status": doc_status})

        # Escalonamento (docs/03_ETAPAS.md §5.3): auto/visao explícito, doc ficou
        # suspeito/ilegível, e itens suficientes para amortizar o custo por página.
        if (estrategia != "visao" and params.estrategia in ("auto", "visao")
                and doc_status in ("suspeito", "ilegivel") and len(itens_doc) >= params.limiar_visao):
            extraidos_v = _processar_estrategia("visao", curador_factory, texto_doc, pasta, itens_doc, params)
            doc_status_v = estr_base.doc_status_de_motivos({
                ik: estr_base.validar_extracao(ex, next(
                    it for it in itens_doc if it["item_key"] == ik))[0]
                for ik, ex in extraidos_v.items()})
            doc_extracao.append({"numeroControlePNCP": doc_ctrl, "estrategia": "visao",
                                 "n_paginas": len(linhas_paginas), "n_paginas_ocr": n_ocr,
                                 "n_itens_tabela": len(extraidos_v), "chamadas_llm": len(itens_doc),
                                 "doc_status": doc_status_v})
            if doc_status_v == "ok" or doc_status == "ilegivel":
                extraidos, estrategia, doc_status = extraidos_v, "visao", doc_status_v

        linhas = [_linha_enriquecido(it, extraidos.get(it["item_key"], {"encontrado": False}),
                                     estrategia) for it in itens_doc]
        for l in linhas:
            l["doc_status"] = doc_status
            l["destino"] = estr_base.destino_de(l["status"], doc_status)
        return linhas, doc_extracao
    finally:
        if pasta and os.path.isdir(pasta):
            shutil.rmtree(pasta, ignore_errors=True)  # ADR-012: PDF é efêmero


_lock_paginas = threading.Lock()
_escritor_paginas: EscritorSeguro | None = None


def _grava_paginas(linhas: list[dict]) -> None:
    global _escritor_paginas
    if not linhas:
        return
    with _lock_paginas:
        for l in linhas:
            _escritor_paginas.escrever(l)


def consolidar_destino() -> dict:
    """Projeta item_key→destino/doc_status a partir do estado final de `SAIDA` (última linha
    por item_key vence — reprocessar um documento sobrescreve o veredito dos seus itens)."""
    por_item: dict[str, dict] = {}
    for r in ler_csv(str(SAIDA)):
        r["destino"] = estr_base.destino_de(r.get("status", ""), r.get("doc_status", "ok"))
        por_item[r["item_key"]] = r
    linhas = list(por_item.values())
    escrever_csv(str(DESTINO), COLS_DESTINO, [
        {"item_key": l["item_key"], "destino": l["destino"], "doc_status": l.get("doc_status", "")}
        for l in linhas])
    return estr_base.contagem_destinos(linhas)


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    if not SOBREVIVENTES.exists():
        return Estimativa(detalhes={"aviso": f"{SOBREVIVENTES} ausente — rode a etapa 4 antes."})
    grupos, pend, feitos = _documentos_pendentes(params)
    n_itens_pend = sum(len(grupos[d]) for d in pend)
    return Estimativa(
        unidades=len(pend), chamadas_llm=n_itens_pend,
        detalhes={"documentos_visiveis": len(grupos), "documentos_pendentes": len(pend),
                  "itens_nos_pendentes": n_itens_pend, "estrategia": params.estrategia},
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    global _escritor_paginas
    cfg = ctx.config
    msg = exigir(cfg, params.provedor)
    if msg:
        raise SystemExit(msg)
    if not params.pular_ocr and ctx.provedores.resolucao("ocr").origem == "env":
        msg = exigir(cfg, "ocr")
        if msg:
            raise SystemExit(msg)
    if not SOBREVIVENTES.exists():
        raise SystemExit(f"{SOBREVIVENTES} ausente. Rode a etapa 4 antes.")

    grupos, pend, feitos = _documentos_pendentes(params)
    if not pend:
        ctx.log("info", "[5] Nada a fazer (todos os documentos já processados). Consolidando destino…")
        cont = consolidar_destino()
        ctx.log("info", f"[5] Destino: manter={cont.get('manter',0)} descartar="
                        f"{cont.get('descartar',0)} revisar={cont.get('revisar',0)}")
        return ResultadoEtapa(metricas=dict(cont))

    os.makedirs(str(STAGING), exist_ok=True)
    ctx.log("info", f"[bold][5] {len(grupos)} documentos sobreviventes, pendentes: {len(pend)}[/] "
                    f"— estratégia: {params.estrategia}, provedor: {params.provedor}, "
                    f"concorrência: {params.concurrency_docs} docs × {params.concurrency_llm} itens")

    prompts_ativos = {}
    try:
        with db.sessao() as sessao:
            prompts_ativos = prompts_resolver.carregar_ativos(
                sessao, ["extrair_item_pdf", "extrair_tabela_pdf", "extrair_tabela_texto",
                        "casar_item_tabela"])
    except Exception:  # noqa: BLE001 — sem banco configurado, cai no prompt hardcoded
        prompts_ativos = {}

    _tls = threading.local()

    def curador_factory():
        if not hasattr(_tls, "c"):
            _tls.c = Curador.from_provedor(cfg, params.provedor, max_retries=6,
                                           prompts_ativos=prompts_ativos)
        return _tls.c

    provedor_ocr = None if params.pular_ocr else ctx.provedores.ocr

    _escritor_paginas = EscritorSeguro(str(PAGINAS), COLS_PAGINAS)
    n_erros = [0]
    try:
        with EscritorSeguro(str(SAIDA), COLS_ENRIQUECIDOS) as w_enr, \
             EscritorSeguro(str(DOC_EXTRACAO), COLS_DOC_EXTRACAO) as w_doc:

            def fn(doc_ctrl):
                return _processar_documento(doc_ctrl, grupos[doc_ctrl], params, curador_factory,
                                            provedor_ocr)

            def ok(_doc_ctrl, resultado):
                linhas_item, linhas_doc = resultado
                for l in linhas_item:
                    w_enr.escrever(l)
                for l in linhas_doc:
                    w_doc.escrever(l)

            def err(doc_ctrl, exc):
                n_erros[0] += 1
                ctx.erro_item(doc_ctrl, exc)
                ctx.log("aviso", f"[yellow][5] erro em {doc_ctrl}: {str(exc)[:120]}[/]")

            ctx.progresso(0, len(pend), descricao="extraindo (download+OCR+LLM)")
            executar_paralelo(pend, fn, concurrency=params.concurrency_docs, on_result=ok,
                              on_error=err, on_progress=lambda f, t: ctx.progresso(f, t))
    finally:
        _escritor_paginas.fechar()
        _escritor_paginas = None

    cont = consolidar_destino()
    ctx.log("info", f"[bold green][5] Concluído.[/] → {SAIDA}")
    ctx.log("info", f"[5] Destino: manter={cont.get('manter',0)} descartar={cont.get('descartar',0)} "
                    f"revisar={cont.get('revisar',0)} (preço diverge={cont.get('preco_diverge',0)}, "
                    f"suspeito={cont.get('preco_suspeito',0)}, sem preço={cont.get('sem_preco',0)})")

    n_itens_pend = sum(len(grupos[d]) for d in pend)
    return ResultadoEtapa(
        processados=n_itens_pend - n_erros[0], erros=n_erros[0],
        metricas={"documentos_processados": len(pend) - n_erros[0], **cont},
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
