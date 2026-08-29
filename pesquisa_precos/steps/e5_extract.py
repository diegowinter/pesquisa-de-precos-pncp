"""
Etapa 5 — o documento vira uma tabela de itens, e a tabela enriquece os itens da API.

Duas chamadas de LLM por documento (ADR-023):

    baixa o PDF do PNCP → manda o ARQUIVO INTEIRO como anexo (capacidade `extract`)
                        → recebe a TABELA DE ITENS em texto, "as it is"
                        → DESCARTA o PDF (ADR-012: url_pncp preservada, a tabela é o ativo)
                        → grava documento_extracao.tabela_texto
                        → por item da API: casa contra ESSA tabela (capacidade `chat`)
                        → grava item_enriquecido (contrato de saída, lido pelas etapas 6-8)

Substituiu as quatro estratégias plugáveis (`window`/`full`/`vision`/`auto`) e a escalada
automática entre elas. O motivo está na ADR-023: no teste de 2026-08-28 aquele desenho
produziu ZERO itens confirmados em 4.159 documentos, e o roteamento `auto` escalava para
visão em ~57% deles contra um modelo que só aceitava texto. Um caminho só, sem chaveamento.

NÃO fazer: persistir o PDF além do documento (sempre `try/finally` + `shutil.rmtree`); usar
o preço como critério de aceite (é SAÍDA, não filtro — docs/08_CONVENCOES.md §5.9); mandar o
documento inteiro para a chamada de casamento (o ponto das duas passadas é que a segunda vê
só a tabela).
"""

import os
import shutil
import sys
import tempfile
import threading
from collections import defaultdict

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text

from pesquisa_precos.core import extraction as regras
from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.core.parallel import executar_paralelo
from pesquisa_precos.db import session as db
from pesquisa_precos.steps.base import (Cancelada, Estimate, RunContext, StepResult,
                                        avanco_cancelavel)

KEY = "5"
# 3.0.0 (ADR-023): estratégias plugáveis fora; extração direta com o PDF anexo.
CODE_VERSION = "3.0.0"

# Se as primeiras N extrações falharem TODAS, o problema não é do documento — é do provedor
# (modelo que não aceita anexo, chave vencida, serviço fora). Foi a lição da etapa 3 em
# 2026-08-25: sem isto, a etapa segue documento a documento rumo a milhares de falhas.
LIMITE_FALHA_TOTAL = 20


class _FalhaTotal(SystemExit):
    """Aborta a etapa: nada foi extraído e o provedor recusou tudo."""


class Params(BaseModel):
    concurrency_docs: int = Field(
        4, ge=1, le=16, description="Documentos processados em paralelo (download + extração)")
    concurrency_llm: int = Field(
        8, ge=1, le=32, description="Itens casados em paralelo, dentro de um documento")
    max_mb: int = Field(
        32, ge=1, le=200, description="Teto de tamanho do PDF; acima disso o documento é "
                                      "marcado como ilegível sem chamar o modelo")
    file_parser: bool = Field(
        True, description="Pede ao OpenRouter que parseie o PDF (plugin file-parser). "
                          "Desligue se o modelo já lê PDF nativamente ou se o provedor não "
                          "for OpenRouter")
    limite_docs: int | None = Field(None, description="Teto de documentos (debug)")
    documentos: str | None = Field(
        None, description="numeroControlePNCP separados por vírgula — força reprocesso mesmo "
                          "se o documento já estiver extraído")


# O plugin do OpenRouter que converte o PDF anexo em texto antes de o modelo ver. Modelos com
# leitura nativa de documento não precisam dele; para os demais, sem isto o anexo é ignorado
# em silêncio — e a resposta vem plausível e vazia, que é o pior modo de falhar.
PLUGIN_FILE_PARSER = {"plugins": [{"id": "file-parser", "pdf": {"engine": "pdf-text"}}]}


def _num(v):
    """Texto → `Decimal` (colunas de dinheiro são `Numeric`, nunca `float`
    — docs/08_CONVENCOES.md §5.8). Vazio/inválido vira NULL."""
    from decimal import Decimal, InvalidOperation

    s = str(v if v is not None else "").strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


class _Acumulador:
    """Contadores compartilhados entre as threads de documento, com o circuit breaker."""

    def __init__(self):
        self._lock = threading.Lock()
        self.ok = 0
        self.erros = 0

    def registra_erro(self, causa: object = "") -> None:
        with self._lock:
            self.erros += 1
            desistir = self.ok == 0 and self.erros >= LIMITE_FALHA_TOTAL
        if desistir:
            raise _FalhaTotal(
                f"As primeiras {self.erros} extrações falharam e nenhuma deu certo — o "
                f"problema é do provedor de `extract`, não dos documentos. Última causa: "
                f"{causa}. Confira o modelo e a chave em /providers: o modelo precisa "
                f"aceitar PDF anexo. Depois rode a etapa de novo.")

    def registra_ok(self) -> None:
        with self._lock:
            self.ok += 1


def _documentos_pendentes(params: Params) -> tuple[dict, list[str]]:
    """Sobreviventes agrupados por documento, direto do banco.

    Chave de resumo: `documento.estado = 'extraido'` — derivada do próprio dado, como manda o
    ADR-018.
    """
    ok, detalhe = db.is_available()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
    forcados = _documentos_alvo(params)
    with db.session() as s:
        linhas = s.execute(sa_text("""
            SELECT i.item_key, i.numero_controle_pncp, i.numero_item, i.descricao_api,
                   i.unidade, i.quantidade, i.preco_unitario, d.tipo_doc::text, d.orgao_cnpj,
                   d.ano, d.numero_sequencial, d.numero_sequencial_ata, d.estado::text
              FROM item i JOIN documento d USING (numero_controle_pncp)
             WHERE i.sobrevivente
             ORDER BY i.numero_controle_pncp, i.numero_item
        """)).all()
    grupos: dict[str, list[dict]] = defaultdict(list)
    estados: dict[str, str] = {}
    for (ik, nc, n_item, desc, un, qtd, preco, tipo, cnpj, ano, seq, seq_ata, estado) in linhas:
        estados[nc] = estado
        grupos[nc].append({
            "item_key": ik, "numeroControlePNCP": nc, "numeroItem": n_item,
            "descricao_api": desc or "", "unidade": un or "",
            "quantidade": str(qtd or ""), "preco_unitario": str(preco or ""),
            "tipo_doc": tipo, "orgao_cnpj": cnpj or "", "ano": str(ano or ""),
            "numero_sequencial": seq or "", "numero_sequencial_ata": seq_ata or "",
        })
    pend = [nc for nc in grupos if nc in forcados or estados.get(nc) != "extraido"]
    if params.limite_docs:
        pend = pend[: params.limite_docs]
    return grupos, pend


def _documentos_alvo(params: Params) -> set[str]:
    if not params.documentos:
        return set()
    return {d.strip() for d in params.documentos.split(",") if d.strip()}


def _baixar_documento(item0: dict, destino: str) -> list[str]:
    """Baixa os arquivos do documento na pasta `destino`. Devolve os nomes salvos.

    Baixar é I/O barato, e o cliente da API do PNCP já vive aqui (é o mesmo da etapa 2). Antes
    da ADR-023 esta lógica morava no `PdfRemotoAdapter`, que baixava só para reenviar os bytes
    ao serviço de `pdf`; agora os bytes vão direto para o modelo.

    `url_pncp` sozinha aponta para a PÁGINA do documento no portal, não para o arquivo — quem
    resolve o arquivo é `listar_arquivos()`, a partir dos sequenciais que a etapa 2 preserva.
    """
    from pesquisa_precos.core.collection import fetch_files

    tipo = item0.get("tipo_doc") or ""
    cnpj = (item0.get("orgao_cnpj") or "").strip()
    ano = (item0.get("ano") or "").strip()
    seq = (item0.get("numero_sequencial") or "").strip()
    if not all([tipo, cnpj, ano, seq]):
        return []
    arquivos = fetch_files.listar_arquivos(
        tipo, cnpj, ano, seq, (item0.get("numero_sequencial_ata") or "").strip() or None,
        silent=True)
    alvos = fetch_files.selecionar_do_tipo(arquivos, tipo)
    return fetch_files.baixar_arquivos(alvos, destino, silent=True) if alvos else []


def _linha_enriquecido(item: dict, extraido: dict) -> dict:
    status, preco_pdf, div = regras.validar_extracao(extraido, item)
    achou = status.startswith("pdf_ok")
    return {
        "item_key": item["item_key"],
        "descricao_final": (extraido.get("descricao_completa") or "").strip() if achou
                           else item.get("descricao_api", ""),
        "fonte_descricao": "pdf" if achou else "api",
        "preco_api": item.get("preco_unitario", ""),
        "preco_pdf": "" if preco_pdf is None else preco_pdf,
        "divergencia_preco": "" if div is None else div,
        "fornecedor": (extraido.get("fornecedor") or "") if achou else "",
        "quantidade_pdf": extraido.get("quantidade", "") if achou else "",
        "status": status,
        # doc_status/destino são preenchidos depois, quando TODOS os itens do doc são conhecidos
    }


def _sem_tabela(itens_doc: list[dict], n_paginas: int = 0) -> tuple[list[dict], dict]:
    """Documento sem arquivo, grande demais ou sem tabela de itens: nenhum item confirma.

    Grava mesmo assim — `documento_extracao` com `tabela_texto` vazia é o registro de que o
    documento JÁ foi tentado, e é o que impede de repagar o download na próxima execução.
    """
    linhas = [{**_linha_enriquecido(it, {"encontrado": False}),
               "status": "sem_texto", "doc_status": "ilegivel", "destino": "revisar"}
              for it in itens_doc]
    return linhas, {"tabela_texto": "", "n_paginas": n_paginas}


def _processar_documento(doc_ctrl: str, itens_doc: list[dict], params: Params,
                         extrator, curador_factory) -> tuple[list[dict], dict]:
    """Um documento inteiro: baixa → tabela → casa cada item → valida.

    Devolve (linhas de `item_enriquecido`, linha de `documento_extracao`).
    """
    item0 = itens_doc[0]
    pasta = tempfile.mkdtemp(prefix="e5_pdf_")
    try:
        nomes = _baixar_documento(item0, pasta)
        if not nomes:
            return _sem_tabela(itens_doc)
        # O primeiro arquivo é o documento original (`selecionar_do_tipo` ordena por
        # sequencialDocumento); os seguintes são aditivos, que não trazem a tabela de itens.
        caminho = os.path.join(pasta, nomes[0])
        tamanho = os.path.getsize(caminho)
        if tamanho > params.max_mb * 1024 * 1024:
            return _sem_tabela(itens_doc)
        with open(caminho, "rb") as fh:
            pdf_bytes = fh.read()
    finally:
        # ADR-012: o PDF é efêmero. Ele vive o tempo da leitura, e nada mais.
        shutil.rmtree(pasta, ignore_errors=True)

    tabela_texto = extrator().curador.extrair_tabela_documento(pdf_bytes, nomes[0])
    if not tabela_texto.strip():
        return _sem_tabela(itens_doc)

    extraidos: dict[str, dict] = {}

    def casar(item):
        return curador_factory().casar_item_tabela(item, tabela_texto)

    def ok(item, res):
        extraidos[item["item_key"]] = res

    def err(item, _exc):
        extraidos[item["item_key"]] = {"encontrado": False}

    executar_paralelo(itens_doc, casar, concurrency=params.concurrency_llm,
                      on_result=ok, on_error=err)

    linhas = [_linha_enriquecido(it, extraidos.get(it["item_key"], {"encontrado": False}))
              for it in itens_doc]
    doc_status = regras.doc_status_de_motivos({linha["item_key"]: linha["status"]
                                               for linha in linhas})
    for linha in linhas:
        linha["doc_status"] = doc_status
        linha["destino"] = regras.destino_de(linha["status"], doc_status)
    return linhas, {"tabela_texto": tabela_texto, "n_paginas": None}


# ── Gravação ────────────────────────────────────────────────────────────────────────
#
# Cada documento é gravado ASSIM QUE TERMINA, na sua própria transação: a etapa 5 é das mais
# caras do ciclo, e perder uma hora de extração por uma queda no fim seria exatamente o que o
# checkpoint por documento existe para evitar.

def _gravar_documento(doc_ctrl: str, linhas_item: list[dict], extracao: dict,
                      run_id: int | None, model: str, provider: str) -> None:
    from pesquisa_precos.db import copy
    from pesquisa_precos.db.repos import extraction as repo

    doc_status = linhas_item[0]["doc_status"] if linhas_item else "ilegivel"
    # `text` do PostgreSQL recusa byte NUL, e a resposta do modelo carrega o que o
    # parser do provedor tiver deixado passar do PDF — foi assim no texto por página.
    linha_extr = (doc_ctrl, copy.texto_para_pg(extracao.get("tabela_texto") or ""),
                  extracao.get("n_paginas"),
                  # tokens/custo ainda não são medidos por documento; as colunas são NOT NULL
                  # com DEFAULT 0, e `DEFAULT` não vale para NULL enviado explicitamente por
                  # um COPY. Zero é o valor certo — `duration_ms` fica NULL porque "não
                  # medido" não é "zero".
                  0, 0, 0, None, model, provider, run_id)
    lote_itens = [(
        linha["item_key"], linha.get("descricao_final") or "",
        linha.get("fonte_descricao") or "api", _num(linha.get("preco_api")),
        _num(linha.get("preco_pdf")), _num(linha.get("divergencia_preco")),
        linha.get("fornecedor") or None, _num(linha.get("quantidade_pdf")),
        linha.get("status") or "", linha.get("destino") or "revisar",
        regras.estado_documento(linha.get("doc_status")), run_id,
    ) for linha in linhas_item]

    with db.raw_connection() as conn:
        repo.gravar_extracoes(conn, [linha_extr])
        if lote_itens:
            repo.gravar_enriquecidos(conn, lote_itens)
        conn.commit()

    # `documento.estado` é a chave de resumo da etapa (ADR-018). O estado carrega o veredito:
    # `ilegivel`/`suspeito` continuam distinguíveis de `extraido`, e é isso que permite
    # reprocessar só os ruins depois de trocar o modelo.
    from pesquisa_precos.db.repos import documento as repo_doc

    with db.session() as s:
        repo_doc.atualizar_estado(s, [(doc_ctrl, regras.estado_documento(doc_status))])
        s.commit()


def estimate(params: Params, ctx: RunContext) -> Estimate:
    ok, detalhe = db.is_available()
    if not ok:
        return Estimate(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    try:
        grupos, pend = _documentos_pendentes(params)
    except SystemExit as e:
        return Estimate(detalhes={"aviso": str(e)})
    n_itens = sum(len(grupos[d]) for d in pend)
    return Estimate(
        unidades=len(pend),
        # Uma chamada de extração por documento + uma de casamento por item.
        chamadas_llm=len(pend) + n_itens,
        detalhes={"documentos_visiveis": len(grupos),
                  "documentos_pendentes": len(pend),
                  "itens_nos_pendentes": n_itens},
    )


def run(params: Params, ctx: RunContext) -> StepResult:
    grupos, pend = _documentos_pendentes(params)
    if not pend:
        from pesquisa_precos.db.repos import extraction as repo_extr
        with db.session() as s:
            cont = repo_extr.contar(s)
        ctx.log("info", "[5] Nada a fazer (todos os documentos já extraídos).")
        return StepResult(metrics=dict(cont))

    # Resolve as duas capacidades ANTES de baixar qualquer coisa: sem provedor apontado, a
    # etapa para aqui em vez de descobrir o problema no meio do primeiro documento.
    resolucao_extract = ctx.providers.resolucao("extract")
    resolucao_chat = ctx.providers.resolucao("chat")
    model = getattr(resolucao_extract.info, "model", None) or resolucao_extract.info.name

    n_itens = sum(len(grupos[d]) for d in pend)
    ctx.log("info", f"[bold][5] {len(grupos)} documentos sobreviventes, pendentes: "
                    f"{len(pend)} ({n_itens} itens)[/] — extração: {model} "
                    f"({resolucao_extract.info.name}), casamento: "
                    f"{resolucao_chat.info.name}, concorrência: {params.concurrency_docs} "
                    f"docs × {params.concurrency_llm} itens")

    prompts_ativos = {}
    try:
        with db.session() as sessao:
            prompts_ativos = prompts_resolver.carregar_ativos(
                sessao, ["extrair_tabela_documento", "casar_item_tabela"])
    except Exception:  # noqa: BLE001 — sem banco de prompts, cai no hardcoded
        prompts_ativos = {}

    # Um cliente por thread nos dois casos: compartilhar um cliente HTTP serializa as chamadas.
    _tls = threading.local()

    def extrator():
        if not hasattr(_tls, "e"):
            kwargs = {"max_retries": 3, "prompts_ativos": prompts_ativos,
                      # O anexo pode ter dezenas de MB e o documento inteiro passa pelo
                      # modelo: o timeout padrão de 60s não cobre isso.
                      "timeout": 600}
            if params.file_parser:
                kwargs["extra_body"] = dict(PLUGIN_FILE_PARSER)
            _tls.e = ctx.providers.novo_extract(curador_kwargs=kwargs)
        return _tls.e

    def curador_factory():
        if not hasattr(_tls, "c"):
            _tls.c = ctx.providers.novo_chat(curador_kwargs={
                "max_retries": 6, "prompts_ativos": prompts_ativos}).curador
        return _tls.c

    # `run_id` não está no protocolo `RunContext` — o `NullContext` não o tem.
    run_id = getattr(ctx, "run_id", None)
    acumulador = _Acumulador()

    def fn_doc(doc_ctrl):
        return _processar_documento(doc_ctrl, grupos[doc_ctrl], params,
                                    extrator, curador_factory)

    def ok_doc(doc_ctrl, resultado):
        linhas_item, extracao = resultado
        _gravar_documento(doc_ctrl, linhas_item, extracao, run_id, model,
                          resolucao_extract.info.name)
        if extracao.get("tabela_texto"):
            acumulador.registra_ok()
        else:
            # Documento sem tabela não é falha de provedor — está gravado e não volta à fila.
            # Não conta como erro, mas também não conta como sucesso para o circuit breaker.
            ctx.log("aviso", f"[yellow][5] {doc_ctrl}: sem tabela de itens no documento[/]")

    def err_doc(doc_ctrl, exc):
        ctx.item_error(doc_ctrl, exc)
        ctx.log("aviso", f"[yellow][5] erro em {doc_ctrl}: {str(exc)[:160]}[/]")
        acumulador.registra_erro(exc)

    ctx.progresso(0, len(pend), descricao="extraindo tabela (PDF → LLM)")
    try:
        executar_paralelo(pend, fn_doc, concurrency=params.concurrency_docs,
                          on_result=ok_doc, on_error=err_doc,
                          on_progress=avanco_cancelavel(ctx))
    except Cancelada:
        ctx.log("aviso", "[yellow][5] Cancelado pelo operador — os documentos já extraídos "
                         "estão gravados e não voltam ao PDF nem ao modelo.[/]")

    with db.session() as s:
        from pesquisa_precos.db.repos import extraction as repo_extr
        cont = repo_extr.contar(s)
    cor = "yellow" if acumulador.erros else "green"
    ctx.log("info", f"[bold {cor}][5] Concluído.[/] {acumulador.erros} erros. → banco ({cont})")

    return StepResult(
        processed=acumulador.ok, errors=acumulador.erros,
        metrics={"documentos_pendentes": len(pend), "itens_nos_pendentes": n_itens, **cont},
    )
