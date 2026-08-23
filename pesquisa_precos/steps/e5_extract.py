"""
Etapa 5 — Download + extração + enriquecimento, com estratégias plugáveis (Fase 8, ADR-010).

Substitui o par `e5a_ocr.py` (parse/OCR) + `e5b_extrair.py` (janela) e a primeira versão
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
`fetch_files.listar_arquivos()` sem reconsultar a busca).
Saídas: data/5_pdf_texto.csv (texto por página, append, resumível por numeroControlePNCP),
        data/5_itens_enriquecidos.csv (contrato de saída — item_key, descricao_final,
        fonte_descricao, preco_api, preco_pdf, divergencia_preco, fornecedor,
        quantidade_pdf, status, destino, estrategia, doc_status),
        data/5_itens_destino.csv (projeção item_key→destino, o que a 6a consome),
        data/5_documento_extracao.csv (1 linha por (documento, estratégia) — custo/páginas).
Chave de resumo: numeroControlePNCP (documento) — reprocessar um documento sobrescreve o
veredito de TODOS os seus itens; é o
mecanismo por trás de "reprocessar este documento com outra estratégia".

NÃO fazer: persistir o PDF além da vida do worker (sempre `try/finally` + `shutil.rmtree`);
usar o preço como critério de aceite (é SAÍDA, não filtro — docs/08_CONVENCOES.md §5.9);
truncar documento grande em silêncio na estratégia `completa` (usar `strategies.full.
dividir_em_chunks`, que tem overlap).

Uso: python -m pesquisa_precos.steps.e5_extract [--estrategia auto|janela|completa|visao]
     [--provider openrouter|local] [--concurrency-docs 4] [--concurrency-llm 8]
     [--documentos <numeroControlePNCP,...>] [--limite-docs N]
"""

import sys
import threading
from collections import defaultdict

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text
from typing import Literal

from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.core.parallel import executar_paralelo
from pesquisa_precos.db import session as db
from pesquisa_precos.strategies import base as estr_base
from pesquisa_precos.strategies import full as estr_full
from pesquisa_precos.strategies import window as estr_window
from pesquisa_precos.strategies import routing
from pesquisa_precos.strategies import vision as estr_vision
from pesquisa_precos.steps.base import RunContext, Estimate, StepResult

KEY = "5"
CODE_VERSION = "2.0.0"





class Params(BaseModel):
    estrategia: Literal["auto", "window", "full", "vision"] = Field(
        "auto", description="Estratégia de extração; 'auto' roteia por documento (ADR-010)")
    provider: str = Field("openrouter", description="Provedor de LLM [local|openrouter]")
    concurrency_docs: int = Field(
        4, ge=1, le=16, description="Documentos processed em paralelo (download+OCR+extração)")
    concurrency_llm: int = Field(
        8, ge=1, le=32, description="Chamadas de LLM em paralelo por item, dentro de um documento")
    janela_max: int = Field(9000, ge=1000, description="Teto de chars da janela (estratégia janela)")
    raio_preco: int = Field(1500, ge=100, description="Raio ao redor de cada ocorrência do preço")
    tamanho_tabela: int = Field(
        2500, ge=0, description="Estimate de chars da tabela — usado na fórmula do roteamento auto")
    limiar_visao: int = Field(
        3, ge=1, description="Nº mínimo de itens sobreviventes p/ escalar a 'vision' quando o "
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
        from pesquisa_precos.db import copy
        from pesquisa_precos.db.repos import extraction as repo

        lote = [(linha["numeroControlePNCP"], linha["arquivo"], int(linha["pagina"] or 0), linha["fonte"],
                 copy.texto_para_pg(linha["texto"] or ""))
                for linha in linhas]
        with db.raw_connection() as conn:
            repo.gravar_paginas(conn, lote)
            conn.commit()

    def enriquecidos(self, linhas: list[dict]) -> None:
        if not linhas:
            return
        from pesquisa_precos.db.repos import extraction as repo

        lote = [(
            linha["item_key"], linha.get("descricao_final") or "", linha.get("fonte_descricao") or "api",
            _num(linha.get("preco_api")), _num(linha.get("preco_pdf")), _num(linha.get("divergencia_preco")),
            linha.get("fornecedor") or None, _num(linha.get("quantidade_pdf")),
            linha.get("status") or "", linha.get("destino") or "revisar",
            linha.get("estrategia") or "window", linha.get("doc_status") or "ok", self.run_id,
        ) for linha in linhas]
        with db.raw_connection() as conn:
            repo.gravar_enriquecidos(conn, lote)
            conn.commit()

    def extracoes(self, linhas: list[dict]) -> None:
        if not linhas:
            return
        import json

        from pesquisa_precos.db.repos import extraction as repo

        lote = [(
            linha["numeroControlePNCP"], linha["estrategia"],
            json.dumps({"n_itens_tabela": linha.get("n_itens_tabela", 0),
                        "chamadas_llm": linha.get("chamadas_llm", 0),
                        "doc_status": linha.get("doc_status", "")}, ensure_ascii=False),
            linha.get("n_paginas", 0), linha.get("n_paginas_ocr", 0),
            None, None, None, None, None, None, self.run_id,
        ) for linha in linhas]
        with db.raw_connection() as conn:
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

    ok, detalhe = db.is_available()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
    forcados = _documentos_alvo(params)
    with db.session() as s:
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
    """Devolve `item_key -> extraido` (shape de `strategies.base.validar_extracao`).

    `janela` faz UMA chamada de LLM por item — para isso valer a pena, os itens do documento
    são extraídos em paralelo (`concurrency_llm`), e cada thread pega o SEU PRÓPRIO `Curador`
    via `curador_factory()` (thread-local): compartilhar um cliente entre threads serializa as
    chamadas (mesmo cuidado da antiga `e5b_extrair.py`). `completa`/`visao` fazem só 1–2
    chamadas "caras" por documento (tabela) + casamentos baratos; rodam sequenciais na thread
    do documento — o paralelismo de `concurrency_docs` já basta para elas.
    """
    if estrategia == "window":
        def fn(item):
            return estr_window.extrair_item(curador_factory(), texto_doc, item,
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
    if estrategia == "full":
        tabela = estr_full.extrair_tabela(curador, texto_doc)
        return estr_base.casar_itens_contra_tabela(curador, itens_doc, tabela)

    # visao — `imagens_fn` é CHAMADO só aqui, de propósito: rasterizar é caro (200 DPI por
    # página) e a visão é rota de exceção. Nas outras estratégias ele nunca é invocado.
    tabela = estr_vision.extrair_tabela(curador, imagens_fn(), max_paginas=params.max_paginas)
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
        linhas = [{**_linha_enriquecido(it, {"encontrado": False}, "window"),
                  "status": "sem_texto", "doc_status": "ilegivel", "destino": "revisar"}
                 for it in itens_doc]
        doc_extracao.append({"numeroControlePNCP": doc_ctrl, "estrategia": "window",
                             "n_paginas": 0, "n_paginas_ocr": 0, "n_itens_tabela": 0,
                             "chamadas_llm": 0, "doc_status": "ilegivel"})
        return linhas, doc_extracao

        _grava_paginas(linhas_paginas)

        if not texto_doc.strip():
            linhas = [{**_linha_enriquecido(it, {"encontrado": False}, "window"),
                      "status": "sem_texto", "doc_status": "ilegivel", "destino": "revisar"}
                     for it in itens_doc]
            doc_extracao.append({"numeroControlePNCP": doc_ctrl, "estrategia": "window",
                                 "n_paginas": len(linhas_paginas), "n_paginas_ocr": n_ocr,
                                 "n_itens_tabela": 0, "chamadas_llm": 0, "doc_status": "ilegivel"})
            return linhas, doc_extracao

        estrategia = params.estrategia
        if estrategia == "auto":
            estrategia = routing.escolher_estrategia(
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
        if (estrategia != "vision" and params.estrategia in ("auto", "vision")
                and doc_status in ("suspeito", "ilegivel") and len(itens_doc) >= params.limiar_visao):
            extraidos_v = _processar_estrategia("vision", curador_factory, texto_doc, imagens_fn,
                                            itens_doc, params)
            doc_status_v = estr_base.doc_status_de_motivos({
                ik: estr_base.validar_extracao(ex, next(
                    it for it in itens_doc if it["item_key"] == ik))[0]
                for ik, ex in extraidos_v.items()})
            doc_extracao.append({"numeroControlePNCP": doc_ctrl, "estrategia": "vision",
                                 "n_paginas": len(linhas_paginas), "n_paginas_ocr": n_ocr,
                                 "n_itens_tabela": len(extraidos_v), "chamadas_llm": len(itens_doc),
                                 "doc_status": doc_status_v})
            if doc_status_v == "ok" or doc_status == "ilegivel":
                extraidos, estrategia, doc_status = extraidos_v, "vision", doc_status_v

        linhas = [_linha_enriquecido(it, extraidos.get(it["item_key"], {"encontrado": False}),
                                     estrategia) for it in itens_doc]
        for linha in linhas:
            linha["doc_status"] = doc_status
            linha["destino"] = estr_base.destino_de(linha["status"], doc_status)
        return linhas, doc_extracao


_escritor_paginas: "DestinoBanco | None" = None


def _grava_paginas(linhas: list[dict]) -> None:
    """Único ponto de gravação das páginas. Manter um ponto só é o que mantém
    `_processar_documento`, onde mora a regra de negócio, sem nenhum `if` de persistência."""
    if not linhas or _escritor_paginas is None:
        return
    _escritor_paginas.paginas(linhas)   # o COPY já é atômico; não precisa de lock


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

    estados = [(linha["numeroControlePNCP"],
                linha["doc_status"] if linha.get("doc_status") in ("ilegivel", "suspeito")
                else "extraido")
               for linha in linhas_doc]
    with db.session() as s:
        repo_doc.atualizar_estado(s, estados)
        s.commit()




def estimate(params: Params, ctx: RunContext) -> Estimate:
    ok, detalhe = db.is_available()
    if not ok:
        return Estimate(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    try:
        grupos, pend, _ = _documentos_pendentes_banco(params)
    except SystemExit as e:
        return Estimate(detalhes={"aviso": str(e)})
    n_itens = sum(len(grupos[d]) for d in pend)
    return Estimate(
        unidades=len(pend), chamadas_llm=n_itens,
        detalhes={"documentos_visiveis": len(grupos),
                  "documentos_pendentes": len(pend), "itens_nos_pendentes": n_itens,
                  "estrategia": params.estrategia},
    )


def run(params: Params, ctx: RunContext) -> StepResult:
    global _escritor_paginas
    # OCR não é configurado aqui: quem o chama é o serviço de `pdf`, na máquina dele
    # (ADR-021). Se o OCR estiver mal configurado linhaá, `/health` do serviço acusa
    # `ocr_configurado: false` e a página escaneada volta com o texto nativo.
    grupos, pend, feitos = _documentos_pendentes_banco(params)
    if not pend:
        # Não existe "consolidar destino": `item_enriquecido.destino` já É a projeção que o
        # antigo `5_itens_destino.csv` reconstruía a cada execução.
        from pesquisa_precos.db.repos import extraction as repo_extr
        with db.session() as s:
            cont = repo_extr.contar(s)
        ctx.log("info", "[5] Nada a fazer (todos os documentos já extraídos).")
        return StepResult(metrics=dict(cont))

    ctx.log("info", f"[bold][5] {len(grupos)} documentos sobreviventes, pendentes: {len(pend)}[/] "
                    f"— estratégia: {params.estrategia}, provider: {params.provider}, "
                    f"concorrência: {params.concurrency_docs} docs × {params.concurrency_llm} itens")

    prompts_ativos = {}
    try:
        with db.session() as sessao:
            prompts_ativos = prompts_resolver.carregar_ativos(
                sessao, ["extrair_item_pdf", "extrair_tabela_pdf", "extrair_tabela_texto",
                        "casar_item_tabela"])
    except Exception:  # noqa: BLE001 — sem banco configurado, cai no prompt hardcoded
        prompts_ativos = {}

    _tls = threading.local()

    def curador_factory():
        if not hasattr(_tls, "c"):
            _tls.c = ctx.providers.novo_chat(curador_kwargs={
                "max_retries": 6, "prompts_ativos": prompts_ativos}).curador
        return _tls.c

    # ADR-019: a etapa não conhece mais PyMuPDF nem o servidor de OCR. Ela pede texto à
    # capacidade `pdf`, que decide se o trabalho acontece aqui ou num serviço externo.
    provedor_pdf = ctx.providers.pdf

    n_erros = [0]

    def fn_doc(doc_ctrl):
        return _processar_documento(doc_ctrl, grupos[doc_ctrl], params,
                                    curador_factory, provedor_pdf)

    def err_doc(doc_ctrl, exc):
        n_erros[0] += 1
        ctx.item_error(doc_ctrl, exc)
        ctx.log("aviso", f"[yellow][5] erro em {doc_ctrl}: {str(exc)[:120]}[/]")

    from pesquisa_precos.db.repos import extraction as repo_extr

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

    with db.session() as s:
        cont = repo_extr.contar(s)
    ctx.log("info", f"[bold green][5] Concluído.[/] → banco ({cont})")
    n_itens = sum(len(grupos[d]) for d in pend)
    return StepResult(
        processed=n_itens - n_erros[0], errors=n_erros[0],
        metrics={"documentos_processados": len(pend) - n_erros[0], **cont},
    )
