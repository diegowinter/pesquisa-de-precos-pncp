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
from sqlalchemy import text as sa_text
from typing import Literal

from pesquisa_precos.config import paths
from pesquisa_precos.config.settings import exigir
from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.core.io_seguro import EscritorSeguro, ler_chaves_concluidas, ler_csv, escrever_csv
from pesquisa_precos.core.paralelo import executar_paralelo
from pesquisa_precos.db import sessao as db
from pesquisa_precos.estrategias import base as estr_base
from pesquisa_precos.estrategias import completa as estr_completa
from pesquisa_precos.estrategias import janela as estr_janela
from pesquisa_precos.estrategias import roteamento
from pesquisa_precos.estrategias import visao as estr_visao
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa
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
    fonte: Literal["banco", "csv"] = Field(
        "banco", description="De onde vêm os sobreviventes e para onde vai a extração")
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


# ── Destino da etapa: CSV ou banco (Fase 10) ────────────────────────────────────────
#
# As três saídas (páginas, enriquecidos, extrações) são gravadas pelo MESMO ponto nos dois
# caminhos. Sem isso, `_processar_documento` precisaria saber onde está gravando — e ele é a
# função que concentra a regra de negócio, justamente a que não pode ganhar `if fonte ==`.

class DestinoBanco:
    """Escreve em `documento_pagina`, `item_enriquecido` e `documento_extracao`.

    Cada documento é gravado ASSIM QUE TERMINA, na sua própria transação: a etapa 5 é a mais
    cara do ciclo depois da 3, e perder uma hora de OCR por uma queda no fim seria o mesmo
    erro que o checkpoint por documento existe para evitar.
    """

    def __init__(self, run_id: int | None = None):
        self.run_id = run_id
        self._lock = threading.Lock()

    def paginas(self, linhas: list[dict]) -> None:
        if not linhas:
            return
        from pesquisa_precos.db import copia
        from pesquisa_precos.db.repos import extracao as repo

        lote = [(l["numeroControlePNCP"], l["arquivo"], int(l["pagina"] or 0), l["fonte"],
                 copia.texto_para_pg(l["texto"] or ""))
                for l in linhas]
        with db.conexao_bruta() as conn:
            repo.gravar_paginas(conn, lote)
            conn.commit()

    def enriquecidos(self, linhas: list[dict]) -> None:
        if not linhas:
            return
        from pesquisa_precos.db.repos import extracao as repo

        lote = [(
            l["item_key"], l.get("descricao_final") or "", l.get("fonte_descricao") or "api",
            _num(l.get("preco_api")), _num(l.get("preco_pdf")), _num(l.get("divergencia_preco")),
            l.get("fornecedor") or None, _num(l.get("quantidade_pdf")),
            l.get("status") or "", l.get("destino") or "revisar",
            l.get("estrategia") or "janela", l.get("doc_status") or "ok", self.run_id,
        ) for l in linhas]
        with db.conexao_bruta() as conn:
            repo.gravar_enriquecidos(conn, lote)
            conn.commit()

    def extracoes(self, linhas: list[dict]) -> None:
        if not linhas:
            return
        import json

        from pesquisa_precos.db.repos import extracao as repo

        lote = [(
            l["numeroControlePNCP"], l["estrategia"],
            json.dumps({"n_itens_tabela": l.get("n_itens_tabela", 0),
                        "chamadas_llm": l.get("chamadas_llm", 0),
                        "doc_status": l.get("doc_status", "")}, ensure_ascii=False),
            l.get("n_paginas", 0), l.get("n_paginas_ocr", 0),
            None, None, None, None, None, None, self.run_id,
        ) for l in linhas]
        with db.conexao_bruta() as conn:
            repo.gravar_extracoes(conn, lote)
            conn.commit()

    def fechar(self) -> None:
        pass


def _num(v):
    """Texto do CSV → `Decimal` (colunas de dinheiro são `Numeric`, nunca `float`
    — docs/08_CONVENCOES.md §5.8). Vazio/inválido vira NULL."""
    from decimal import Decimal, InvalidOperation

    s = str(v if v is not None else "").strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _documentos_pendentes_banco(params: Params) -> tuple[dict, list[str], set[str]]:
    """Sobreviventes agrupados por documento, direto do banco.

    Chave de resumo: `documento.estado = 'extraido'`. Derivada do próprio dado, como manda o
    ADR-018 — e mais correta que o CSV, onde "item na saída" e "documento processado" podiam
    divergir se a queda acontecesse entre as duas gravações.
    """

    ok, detalhe = db.esta_disponivel()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env "
                         f"ou rode com --fonte csv.")
    forcados = _documentos_alvo(params)
    with db.sessao() as s:
        linhas = s.execute(sa_text("""
            SELECT i.item_key, i.numero_controle_pncp, i.descricao_api, i.unidade,
                   i.quantidade, i.preco_unitario, d.tipo_doc::text, d.orgao_cnpj, d.ano,
                   d.numero_sequencial, d.numero_sequencial_ata, d.url_pncp, d.estado::text
              FROM item i JOIN documento d USING (numero_controle_pncp)
             WHERE i.sobrevivente
             ORDER BY i.numero_controle_pncp, i.numero_item
        """)).all()
    grupos: dict[str, list[dict]] = defaultdict(list)
    estados: dict[str, str] = {}
    for (ik, nc, desc, un, qtd, preco, tipo, cnpj, ano, seq, seq_ata, url, estado) in linhas:
        estados[nc] = estado
        grupos[nc].append({
            "item_key": ik, "numeroControlePNCP": nc, "descricao_api": desc or "",
            "unidade": un or "", "quantidade": str(qtd or ""),
            "preco_unitario": str(preco or ""), "tipo_doc": tipo,
            "orgao_cnpj": cnpj or "", "ano": str(ano or ""),
            "numero_sequencial": seq or "", "numero_sequencial_ata": seq_ata or "",
            "url_pncp": url or "",
        })
    pend = [nc for nc in grupos if nc in forcados or estados.get(nc) != "extraido"]
    if params.limite_docs:
        pend = pend[: params.limite_docs]
    return grupos, pend, set()


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


def _identificadores(doc_ctrl: str, item0: dict) -> dict:
    """Os campos que a capacidade `pdf` precisa para achar o arquivo (ADR-012).

    `url_pncp` sozinha aponta para a PÁGINA do documento no portal, não para o PDF — quem
    resolve o arquivo é `listar_arquivos()`, a partir dos sequenciais que a etapa 2 preserva
    de propósito desde a Fase 8.
    """
    ano = str(item0.get("ano") or "").strip()
    return {
        "numero_controle": doc_ctrl,
        "tipo_doc": item0.get("tipo_doc", ""),
        "numero_sequencial": (item0.get("numero_sequencial") or "").strip() or None,
        "numero_sequencial_ata": (item0.get("numero_sequencial_ata") or "").strip() or None,
        "orgao_cnpj": (item0.get("orgao_cnpj") or "").strip() or None,
        "ano": int(ano) if ano.isdigit() else None,
    }


def _extrair_texto(provedor_pdf, doc_ctrl: str, item0: dict,
                   pular_ocr: bool) -> tuple[str, list[dict], int]:
    """Documento → (texto concatenado, linhas de página, nº de páginas por OCR).

    Fase 11 (ADR-019): substitui `_baixar_pdfs` + `_parsear_e_ocr`, que baixavam o PDF numa
    pasta local e rodavam PyMuPDF aqui dentro. Agora quem baixa, parseia, rasteriza e chama o
    OCR é a capacidade `pdf` — a etapa recebe texto. Com `PDF_BASE_URL` vazio o trabalho
    acontece em processo, exatamente como antes; com serviço configurado, o container nunca vê
    o PDF.
    """
    url = (item0.get("url_pncp") or "").strip()
    resultado = provedor_pdf.extrair(url, **_identificadores(doc_ctrl, item0))
    paginas = resultado.get("paginas") or []
    if pular_ocr:
        # A capacidade já devolveu tudo; aqui só se descarta o que veio de OCR. Pedir ao
        # provedor para não fazer OCR seria melhor, mas nem todo provedor honra a flag — e
        # filtrar aqui garante o comportamento independentemente de quem atendeu.
        paginas = [pg for pg in paginas if pg.get("fonte") != "ocr"]
    linhas_paginas = [{
        "numeroControlePNCP": doc_ctrl, "arquivo": pg.get("arquivo", ""),
        "pagina": pg.get("pagina", 0), "fonte": pg.get("fonte", "nativo"),
        "texto": pg.get("texto", ""),
    } for pg in paginas]
    ordenadas = sorted(paginas, key=lambda x: (x.get("arquivo", ""), x.get("pagina", 0)))
    texto_doc = "\n".join(pg.get("texto", "") for pg in ordenadas)
    return texto_doc, linhas_paginas, int(resultado.get("n_ocr") or 0)


def _processar_estrategia(estrategia: str, curador_factory, texto_doc: str, imagens_fn,
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

    # visao — `imagens_fn` é CHAMADO só aqui, de propósito: rasterizar é caro (200 DPI por
    # página) e a visão é rota de exceção. Nas outras estratégias ele nunca é invocado.
    tabela = estr_visao.extrair_tabela(curador, imagens_fn(), max_paginas=params.max_paginas)
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
                         curador_factory, provedor_pdf) -> tuple[list[dict], list[dict]]:
    """Processa UM documento inteiro: baixa → parseia/OCR → descarta PDF → roteia estratégia →
    extrai → valida. Devolve (linhas_item_enriquecido_sem_doc_status, linhas_documento_extracao).
    `linhas_documento_extracao` pode ter 2 entradas quando há escalonamento para `visao`.
    """
    doc_extracao: list[dict] = []
    item0 = itens_doc[0]

    def imagens_fn():
        """Páginas rasterizadas, só se a estratégia `visao` for de fato usada."""
        return provedor_pdf.rasterizar(
            (item0.get("url_pncp") or "").strip(), max_paginas=params.max_paginas,
            **_identificadores(doc_ctrl, item0))

    texto_doc, linhas_paginas, n_ocr = _extrair_texto(
        provedor_pdf, doc_ctrl, item0, params.pular_ocr)
    if not linhas_paginas:
        linhas = [{**_linha_enriquecido(it, {"encontrado": False}, "janela"),
                  "status": "sem_texto", "doc_status": "ilegivel", "destino": "revisar"}
                 for it in itens_doc]
        doc_extracao.append({"numeroControlePNCP": doc_ctrl, "estrategia": "janela",
                             "n_paginas": 0, "n_paginas_ocr": 0, "n_itens_tabela": 0,
                             "chamadas_llm": 0, "doc_status": "ilegivel"})
        return linhas, doc_extracao

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

        extraidos = _processar_estrategia(estrategia, curador_factory, texto_doc, imagens_fn,
                                        itens_doc, params)
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
            extraidos_v = _processar_estrategia("visao", curador_factory, texto_doc, imagens_fn,
                                            itens_doc, params)
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


_lock_paginas = threading.Lock()
_escritor_paginas: EscritorSeguro | None = None


def _grava_paginas(linhas: list[dict]) -> None:
    """Único ponto de gravação das páginas — atende CSV e banco (ver `DestinoBanco`).

    Manter um ponto só é o que impede `_processar_documento`, onde mora a regra de negócio, de
    ganhar um `if fonte ==` no meio.
    """
    global _escritor_paginas
    if not linhas or _escritor_paginas is None:
        return
    if isinstance(_escritor_paginas, DestinoBanco):
        _escritor_paginas.paginas(linhas)   # o COPY já é atômico; não precisa do lock
        return
    with _lock_paginas:
        for l in linhas:
            _escritor_paginas.escrever(l)


def _marcar_documento_extraido(linhas_doc: list[dict]) -> None:
    """`documento.estado` é a chave de resumo da etapa no banco (ADR-018).

    Sem isso o documento voltaria à fila na execução seguinte, repagando OCR e LLM — que é
    exatamente o gasto que o checkpoint por documento existe para evitar. O estado carrega o
    veredito: `ilegivel`/`suspeito` continuam distinguíveis de `extraido`, e é isso que
    permite reprocessar só os ruins com outra estratégia.
    """
    if not linhas_doc:
        return
    from pesquisa_precos.db.repos import documento as repo_doc

    estados = [(l["numeroControlePNCP"],
                l["doc_status"] if l.get("doc_status") in ("ilegivel", "suspeito")
                else "extraido")
               for l in linhas_doc]
    with db.sessao() as s:
        repo_doc.atualizar_estado(s, estados)
        s.commit()


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
    if params.fonte == "banco":
        ok, detalhe = db.esta_disponivel()
        if not ok:
            return Estimativa(detalhes={"aviso": f"banco indisponível: {detalhe}"})
        try:
            grupos, pend, _ = _documentos_pendentes_banco(params)
        except SystemExit as e:
            return Estimativa(detalhes={"fonte": "banco", "aviso": str(e)})
        n_itens = sum(len(grupos[d]) for d in pend)
        return Estimativa(
            unidades=len(pend), chamadas_llm=n_itens,
            detalhes={"fonte": "banco", "documentos_visiveis": len(grupos),
                      "documentos_pendentes": len(pend), "itens_nos_pendentes": n_itens,
                      "estrategia": params.estrategia},
        )

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
    if params.fonte == "csv" and not SOBREVIVENTES.exists():
        raise SystemExit(f"{SOBREVIVENTES} ausente. Rode a etapa 4 antes.")

    grupos, pend, feitos = (_documentos_pendentes_banco(params) if params.fonte == "banco"
                            else _documentos_pendentes(params))
    if not pend:
        if params.fonte == "banco":
            # No banco não existe "consolidar destino": `item_enriquecido.destino` já É a
            # projeção que o CSV `5_itens_destino.csv` reconstruía a cada execução.
            from pesquisa_precos.db.repos import extracao as repo_extr
            with db.sessao() as s:
                cont = repo_extr.contar(s)
            ctx.log("info", "[5] Nada a fazer (todos os documentos já extraídos).")
            return ResultadoEtapa(metricas=dict(cont))
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

    # ADR-019: a etapa não conhece mais PyMuPDF nem o servidor de OCR. Ela pede texto à
    # capacidade `pdf`, que decide se o trabalho acontece aqui ou num serviço externo.
    provedor_pdf = ctx.provedores.pdf

    n_erros = [0]

    def fn_doc(doc_ctrl):
        return _processar_documento(doc_ctrl, grupos[doc_ctrl], params,
                                    curador_factory, provedor_pdf)

    def err_doc(doc_ctrl, exc):
        n_erros[0] += 1
        ctx.erro_item(doc_ctrl, exc)
        ctx.log("aviso", f"[yellow][5] erro em {doc_ctrl}: {str(exc)[:120]}[/]")

    if params.fonte == "banco":
        from pesquisa_precos.db.repos import extracao as repo_extr

        destino = DestinoBanco()
        _escritor_paginas = destino    # `_grava_paginas` grava por ele (ver a função)

        def ok_banco(_doc_ctrl, resultado):
            linhas_item, linhas_doc = resultado
            destino.enriquecidos(linhas_item)
            destino.extracoes(linhas_doc)
            _marcar_documento_extraido(linhas_doc)

        try:
            ctx.progresso(0, len(pend), descricao="extraindo (PDF+LLM)")
            executar_paralelo(pend, fn_doc, concurrency=params.concurrency_docs,
                              on_result=ok_banco, on_error=err_doc,
                              on_progress=lambda f, t: ctx.progresso(f, t))
        finally:
            _escritor_paginas = None

        with db.sessao() as s:
            cont = repo_extr.contar(s)
        ctx.log("info", f"[bold green][5] Concluído.[/] → banco ({cont})")
        n_itens = sum(len(grupos[d]) for d in pend)
        return ResultadoEtapa(
            processados=n_itens - n_erros[0], erros=n_erros[0],
            metricas={"documentos_processados": len(pend) - n_erros[0], **cont},
        )

    _escritor_paginas = EscritorSeguro(str(PAGINAS), COLS_PAGINAS)
    try:
        with EscritorSeguro(str(SAIDA), COLS_ENRIQUECIDOS) as w_enr, \
             EscritorSeguro(str(DOC_EXTRACAO), COLS_DOC_EXTRACAO) as w_doc:

            def ok(_doc_ctrl, resultado):
                linhas_item, linhas_doc = resultado
                for l in linhas_item:
                    w_enr.escrever(l)
                for l in linhas_doc:
                    w_doc.escrever(l)

            ctx.progresso(0, len(pend), descricao="extraindo (download+OCR+LLM)")
            executar_paralelo(pend, fn_doc, concurrency=params.concurrency_docs, on_result=ok,
                              on_error=err_doc, on_progress=lambda f, t: ctx.progresso(f, t))
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
