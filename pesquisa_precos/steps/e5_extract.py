"""
Etapa 5 — o documento vira uma tabela de itens, e a tabela enriquece os itens da API.

Duas chamadas de LLM por documento (ADR-023):

    baixa o PDF do PNCP → manda o ARQUIVO INTEIRO como anexo (capacidade `extract`)
                        → recebe a TABELA DE ITENS em texto, "as it is"
                        → DESCARTA o PDF (ADR-012: url_pncp preservada, a tabela é o ativo)
                        → grava documento_extracao.tabela_texto
                        → UMA chamada com os candidatos da compra (capacidade `chat`)
                        → grava item_enriquecido (contrato de saída, lido pelas etapas 6-8)

Os CANDIDATOS de um documento são os itens sobreviventes da COMPRA dele, não "os itens dele"
(ADR-024): a API do PNCP entrega itens por compra e não sabe dizer qual ata registrou qual
item. Um pregão gera N atas, cada uma com o que um fornecedor ganhou — e é esta etapa que
descobre a divisão, gravando em `item_enriquecido.numero_controle_pncp`. Por isso é NORMAL a
maioria dos candidatos não estar num documento: 3 confirmados em 82 é resultado bom, não
suspeito.

Substituiu as quatro estratégias plugáveis (`window`/`full`/`vision`/`auto`) e a escalada
automática entre elas. O motivo está na ADR-023: no teste de 2026-08-28 aquele desenho
produziu ZERO itens confirmados em 4.159 documentos, e o roteamento `auto` escalava para
visão em ~57% deles contra um modelo que só aceitava texto. Um caminho só, sem chaveamento.

NÃO fazer: persistir o PDF além do documento (sempre `try/finally` + `shutil.rmtree`); usar
o preço como critério de aceite (é SAÍDA, não filtro — docs/08_CONVENCOES.md §5.9); mandar o
documento inteiro para a chamada de casamento (o ponto das duas passadas é que a segunda vê
só a tabela).
"""

import hashlib
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
from typing import Literal
from sqlalchemy import text as sa_text

from pesquisa_precos.core import extraction as regras
from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.core.parallel import executar_paralelo
from pesquisa_precos.db import session as db
from pesquisa_precos.steps.base import (Cancelada, Estimate, RunContext, StepResult,
                                        avanco_cancelavel, sem_reasoning)

KEY = "5"
# 3.0.0 (ADR-023): estratégias plugáveis fora; extração direta com o PDF anexo.
# 4.0.0 (ADR-024): item pertence à compra; casamento vira uma chamada por documento.
# 4.1.0: `pdf_engine` volta a NÃO declarar plugin (`auto`) — `cloudflare-ai` entregava 557 de
#        17.878 tokens; falha permanente sai da fila; `doc_status` gravado cru; `llm_call`.
# 4.2.0: `provider_sort` — o OpenRouter roteava para Parasail a ~US$ 2,80/Mtok em vez de
#        Cloudflare a ~US$ 0,10/Mtok. Mesmo resultado, 27× a fatura.
CODE_VERSION = "4.2.0"

# Se as primeiras N extrações falharem TODAS, o problema não é do documento — é do provedor
# (modelo que não aceita anexo, chave vencida, serviço fora). Foi a lição da etapa 3 em
# 2026-08-25: sem isto, a etapa segue documento a documento rumo a milhares de falhas.
LIMITE_FALHA_TOTAL = 20


class DownloadFalhou(RuntimeError):
    """O PNCP publicou o arquivo, mas ele não veio.

    Existe para NÃO deixar isso virar "documento ilegível". `baixar_arquivo` tenta 5 vezes e
    devolve `None` em silêncio; antes de 2026-08-30 `_baixar_documento` devolvia lista vazia e
    o documento recebia o mesmo veredito de um PDF que o modelo leu e não entendeu — sem erro
    na tela, sem custo de LLM, e sem nada que dissesse que o modelo nunca tinha visto o
    arquivo. Foi o que aconteceu com os 1.274 documentos da execução de 2026-08-30: TODOS
    tinham `hash_arquivo` nulo, ou seja, nenhum chegou ao modelo.
    """


class _FalhaTotal(SystemExit):
    """Aborta a etapa: nada foi extraído e o provedor recusou tudo."""


class Params(BaseModel):
    concurrency_docs: int = Field(
        4, ge=1, le=16, description="Documentos processados em paralelo (download + extração)")
    max_mb: int = Field(
        32, ge=1, le=200, description="Teto de tamanho do PDF; acima disso o documento é "
                                      "marcado como ilegível sem chamar o modelo")
    pdf_engine: Literal["auto", "pdf-text", "cloudflare-ai", "mistral-ocr", "native"] = Field(
        "auto",
        description="Como o OpenRouter converte o PDF antes do modelo ver. 'auto' NÃO declara "
                    "plugin e deixa o OpenRouter escolher — é o que o playground faz, e o que "
                    "entregou o documento inteiro de graça. 'pdf-text' extrai a camada de "
                    "texto. 'mistral-ocr' custa US$ 2 por 1.000 páginas e lê documento "
                    "escaneado. 'cloudflare-ai' entregou 567 tokens de um documento de 18.730 "
                    "— não use sem medir. 'native' só serve para modelo que aceita 'file'")
    provider_sort: Literal["price", "throughput", "latency", "livre"] = Field(
        "price",
        description="Como o OpenRouter escolhe o PROVEDOR de inferência do modelo. 'livre' "
                    "deixa ele decidir — e foi assim que a mesma chamada saiu 27× mais cara "
                    "(Parasail a ~US$ 2,80/Mtok em vez de Cloudflare a ~US$ 0,10/Mtok)")
    teto_chars_lote: int = Field(
        40_000, ge=2_000, description="Teto de texto dos itens candidatos por chamada de "
                                      "casamento; acima disso a compra é dividida em lotes")
    reprocessar_ilegiveis: bool = Field(
        False, description="Traz de volta à fila os documentos de que não saiu tabela. Só "
                           "marque junto com uma TROCA de 'pdf_engine' ou de modelo — repetir "
                           "com a mesma configuração paga de novo pelo mesmo resultado")
    limite_docs: int | None = Field(None, description="Teto de documentos (debug)")
    documentos: str | None = Field(
        None, description="numeroControlePNCP separados por vírgula — força reprocesso mesmo "
                          "se o documento já estiver extraído")


def _plugin_pdf(engine: str) -> dict:
    """O plugin do OpenRouter que converte o PDF anexo antes de o modelo ver.

    `auto` NÃO declara plugin. Isso é deliberado, e é o padrão desde 2026-08-30.

    O raciocínio original era o oposto: declarar sempre, porque o silêncio significaria
    `mistral-ocr` a US$ 2/1.000 páginas — uns US$ 76 no acervo — já que o Gemma não declara
    `file` entre os `input_modalities`. A medição desmentiu isso. A MESMA ata, pelo caminho
    sem plugin, chegou ao modelo com **18.730 tokens** e `usage_file: null` — nenhuma cobrança
    de parser. Declarando `cloudflare-ai`, chegou com **567**, e o modelo respondeu
    `SEM_TABELA`, como responderia qualquer um diante de meia página.

    O custo de errar aqui é invisível: a etapa não falha, ela só devolve documento vazio. Foi
    assim que 1.156 de 1.165 extrações "deram certo" produzindo nada.
    """
    if engine == "auto":
        return {}
    return {"plugins": [{"id": "file-parser", "pdf": {"engine": engine}}]}


def _roteamento(sort: str) -> dict:
    """Preferência de PROVEDOR de inferência — outra coisa que o silêncio decide por nós.

    O mesmo modelo é servido por vários provedores a preços muito diferentes, e sem `provider`
    no corpo o OpenRouter escolhe. Em 2026-08-30 ele mandou a etapa para a **Parasail**, a
    ~US$ 2,80/Mtok, enquanto o playground caía na **Cloudflare** a ~US$ 0,10/Mtok — 27× de
    diferença, invisível para nós porque nada falha: a resposta vem igual, só a fatura muda.

    `price` pede o mais barato que sirva o modelo. É o padrão porque este projeto não tem
    orçamento para o caminho caro, e a etapa 5 é a que mais consome token do ciclo.
    """
    if sort == "livre":
        return {}
    return {"provider": {"sort": sort}}


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
        self.permanentes = 0

    def registra_erro(self, causa: object = "", *, permanente: bool = False) -> None:
        """Conta o erro e, se NADA deu certo até aqui, corta a etapa.

        `permanente` é o veredito de `regras.falha_permanente`: o provedor recusou ESTE
        arquivo. Isso conta como erro na tela, mas NÃO alimenta o circuit breaker — ele
        existe para pegar chave errada e modelo sem suporte a PDF, e em 2026-08-30 abortou a
        etapa dizendo "o problema é do provedor" depois de 20 PDFs grandes demais seguidos.
        """
        with self._lock:
            self.erros += 1
            self.permanentes += permanente
            desistir = not permanente and self.ok == 0 and self.erros >= LIMITE_FALHA_TOTAL
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
        # ADR-024: o item pertence à COMPRA. Um documento recebe os itens sobreviventes da
        # compra dele como CANDIDATOS — quais estão de fato nele é o que esta etapa descobre.
        linhas = s.execute(sa_text("""
            SELECT i.item_key, d.numero_controle_pncp, i.numero_item, i.descricao_api,
                   i.unidade, i.quantidade, i.preco_unitario, d.tipo_doc::text, d.orgao_cnpj,
                   d.ano, d.numero_sequencial, d.numero_sequencial_ata, d.estado::text
              FROM documento d JOIN item i ON i.compra_key = d.compra_key
             WHERE i.sobrevivente
             ORDER BY d.numero_controle_pncp, i.numero_item
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
    # `descoberto` é o que nunca foi tentado. `ilegivel` já foi — e repeti-lo com a mesma
    # configuração baixa o mesmo PDF e chama o mesmo modelo para chegar à mesma resposta.
    # Ele volta por escolha explícita (`reprocessar_ilegiveis`), que é quando trocar de
    # engine ou de modelo pode mudar alguma coisa.
    feitos = {"extraido"} if params.reprocessar_ilegiveis else {"extraido", "ilegivel"}
    pend = [nc for nc in grupos if nc in forcados or estados.get(nc) not in feitos]
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
    if not alvos:
        # O órgão não publicou arquivo deste tipo. Não há o que ler, e repetir não muda —
        # este é o caso legítimo de documento sem tabela.
        return []
    nomes = fetch_files.baixar_arquivos(alvos, destino, silent=True)
    if not nomes:
        raise DownloadFalhou(
            f"o PNCP lista {len(alvos)} arquivo(s) para este documento, mas nenhum baixou")
    return nomes


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


def _sem_tabela(itens_doc: list[dict], hash_arquivo: str | None = None,
                uso_extract: tuple[int, int] = (0, 0)) -> tuple[list[dict], dict]:
    """Documento sem arquivo, grande demais ou sem tabela de itens: nenhum item confirma.

    Grava mesmo assim — `documento_extracao` com `tabela_texto` vazia é o registro de que o
    documento JÁ foi tentado, e é o que impede de repagar o download na próxima execução.

    `hash_arquivo` é o que separa os dois "sem tabela" que aqui viram a mesma linha:

    | hash | o que houve |
    |---|---|
    | preenchido | os bytes foram ao modelo, e ele respondeu que não há tabela |
    | nulo | nem chegou lá — sem arquivo publicado, ou grande demais |

    Ele custa nada (já está em memória) e é a única evidência de que a extração aconteceu:
    a `tabela_texto` é vazia nos dois casos. Em 2026-08-30 a ausência dele me levou a
    diagnosticar 1.274 documentos como "nunca chegaram ao modelo" sem ter como saber.
    """
    linhas = [{**_linha_enriquecido(it, {"encontrado": False}),
               "status": "sem_texto", "doc_status": "ilegivel", "destino": "revisar"}
              for it in itens_doc]
    return linhas, {"tabela_texto": "", "hash_arquivo": hash_arquivo,
                    "uso_extract": uso_extract, "uso_chat": (0, 0)}


def _lotes_de_itens(itens_doc: list[dict], teto_chars: int) -> list[list[dict]]:
    """Divide os candidatos em lotes que caibam numa chamada.

    Medido no acervo: mediana de 4 candidatos por compra, média 7, p95 22 — a esmagadora
    maioria vai num lote só. O teto existe para as 26 compras cujas descrições da API somam
    mais de 60k chars; sem ele, elas fariam uma chamada gigante que o modelo trunca em
    silêncio, e truncar aqui é perder item sem erro.
    """
    lotes: list[list[dict]] = []
    atual: list[dict] = []
    tamanho = 0
    for item in itens_doc:
        custo = len(item.get("descricao_api") or "") + 60
        if atual and tamanho + custo > teto_chars:
            lotes.append(atual)
            atual, tamanho = [], 0
        atual.append(item)
        tamanho += custo
    if atual:
        lotes.append(atual)
    return lotes


def _processar_documento(doc_ctrl: str, itens_doc: list[dict], params: Params,
                         extrator, curador_factory) -> tuple[list[dict], dict]:
    """Um documento inteiro: baixa → extrai a tabela → casa os candidatos → valida.

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
        if os.path.getsize(caminho) > params.max_mb * 1024 * 1024:
            return _sem_tabela(itens_doc)
        with open(caminho, "rb") as fh:
            pdf_bytes = fh.read()
        # ADR-012: o PDF é descartado, mas o hash fica — é o que permite conferir, ao rebaixar
        # do PNCP, que o arquivo é o mesmo que gerou esta tabela. Custa nada: os bytes já estão
        # em memória para irem ao modelo.
        hash_arquivo = hashlib.sha256(pdf_bytes).hexdigest()
    finally:
        # ADR-012: o PDF é efêmero. Ele vive o tempo da leitura, e nada mais.
        shutil.rmtree(pasta, ignore_errors=True)

    curador_extract = extrator().curador
    tabela_texto = curador_extract.extrair_tabela_documento(pdf_bytes, nomes[0])
    uso_extract = curador_extract.ultimo_uso
    if not tabela_texto.strip():
        # O modelo VIU o PDF e disse que não há tabela. O hash vai junto — é o que distingue
        # esta resposta de um documento que nem foi baixado.
        return _sem_tabela(itens_doc, hash_arquivo, uso_extract)

    # ── Casamento: UMA chamada por documento, não uma por item (ADR-024) ──────────────
    #
    # Os candidatos são os itens sobreviventes da COMPRA, e este documento contém apenas
    # alguns deles. Perguntar item a item fazia 82 perguntas em cada uma das 25 atas do
    # pregão 507 — 71% delas estruturalmente impossíveis de responder com "sim".
    curador = curador_factory()
    achados: dict[int, dict] = {}
    erro_casamento: Exception | None = None
    uso_chat = [0, 0]
    for lote in _lotes_de_itens(itens_doc, params.teto_chars_lote):
        try:
            achados.update(curador.casar_itens_tabela(lote, tabela_texto))
        except Exception as exc:  # noqa: BLE001 — vira status 'erro', não 'nao_encontrado'
            erro_casamento = exc
        # Depois do try: o lote que falhou também consumiu tokens de entrada, e não
        # contabilizá-los subestimaria justo o documento problemático.
        uso_chat[0] += curador.ultimo_uso[0]
        uso_chat[1] += curador.ultimo_uso[1]

    linhas = []
    for item in itens_doc:
        if erro_casamento is not None and item["numeroItem"] not in achados:
            # A chamada quebrou: "não achei" e "não perguntei direito" NÃO podem virar o
            # mesmo status. Foi essa confusão que escondeu a falha em massa da etapa 3.
            linhas.append({**_linha_enriquecido(item, {"encontrado": False}),
                           "status": "erro", "_erro": str(erro_casamento)[:200]})
            continue
        linhas.append(_linha_enriquecido(
            item, achados.get(item["numeroItem"], {"encontrado": False})))

    doc_status = regras.doc_status_de_motivos(
        {linha["item_key"]: linha["status"] for linha in linhas})
    for linha in linhas:
        linha["doc_status"] = doc_status
        linha["destino"] = regras.destino_de(linha["status"], doc_status)
    return linhas, {"tabela_texto": tabela_texto, "hash_arquivo": hash_arquivo,
                    "uso_extract": uso_extract, "uso_chat": tuple(uso_chat),
                    "_erro_casamento": erro_casamento}


# ── Gravação ────────────────────────────────────────────────────────────────────────
#
# Cada documento é gravado ASSIM QUE TERMINA, na sua própria transação: a etapa 5 é das mais
# caras do ciclo, e perder uma hora de extração por uma queda no fim seria exatamente o que o
# checkpoint por documento existe para evitar.

def _custo(info, tokens: tuple[int, int]) -> float:
    """Preço da chamada pelas tarifas cadastradas em `/providers`.

    Zero quando o provedor não tem tarifa preenchida (a GPU caseira, por exemplo) — e zero
    ali é a verdade, não um dado faltando.
    """
    entrada, saida = tokens
    return (entrada * (info.cost_in_per_mtok or 0.0)
            + saida * (info.cost_out_per_mtok or 0.0)) / 1_000_000 + (
        info.cost_usd_per_call or 0.0)


def _contabilizar(doc_ctrl: str, extracao: dict, run_id: int | None,
                  info_extract, info_chat) -> None:
    """Uma linha em `llm_call` por chamada que o documento fez (docs/02_SCHEMA.md §9).

    A tabela existia desde a Fase 3 e estava VAZIA: medir era responsabilidade de quem
    chamava, e ninguém chamava. Sem ela o custo por documento é estimativa, e o teto por run
    (`teto_custo_usd`) não tem em que se basear.

    As duas chamadas do documento aparecem separadas de propósito: a de `extract` manda o PDF
    inteiro e é a cara; a de `chat` manda só texto. Somá-las esconderia qual das duas pesa.
    """
    from pesquisa_precos.db.repos import execution as repo_exec

    chamadas = [("extract", info_extract, extracao.get("uso_extract") or (0, 0)),
                ("chat", info_chat, extracao.get("uso_chat") or (0, 0))]
    with db.session() as s:
        for capacidade, info, tokens in chamadas:
            if not any(tokens):
                continue  # a chamada não aconteceu (documento que nem baixou)
            repo_exec.registrar_llm_chamada(
                s, run_id=run_id, step="5", capability=capacidade, provider=info.name,
                model=info.model, key=doc_ctrl, tokens_in=tokens[0], tokens_out=tokens[1],
                cost_usd=_custo(info, tokens),
                success=capacidade == "extract" or extracao.get("_erro_casamento") is None)
        s.commit()


def _gravar_documento(doc_ctrl: str, linhas_item: list[dict], extracao: dict,
                      run_id: int | None, model: str, provider: str,
                      info_extract=None) -> None:
    from pesquisa_precos.db import copy
    from pesquisa_precos.db.repos import extraction as repo

    doc_status = linhas_item[0]["doc_status"] if linhas_item else "ilegivel"
    # `text` do PostgreSQL recusa byte NUL, e a resposta do modelo carrega o que o
    # parser do provedor tiver deixado passar do PDF — foi assim no texto por página.
    # Só a chamada de EXTRAÇÃO entra aqui: esta linha é o registro do documento, e o
    # casamento é sobre os itens dele. As duas aparecem separadas em `llm_call`.
    # `duration_ms` fica NULL porque "não medido" não é "zero".
    tk_in, tk_out = extracao.get("uso_extract") or (0, 0)
    linha_extr = (doc_ctrl, copy.texto_para_pg(extracao.get("tabela_texto") or ""),
                  tk_in, tk_out,
                  _custo(info_extract, (tk_in, tk_out)) if info_extract else 0,
                  None, model, provider, run_id)
    lote_itens = [(
        linha["item_key"], doc_ctrl, linha.get("descricao_final") or "",
        linha.get("fonte_descricao") or "api", _num(linha.get("preco_api")),
        _num(linha.get("preco_pdf")), _num(linha.get("divergencia_preco")),
        linha.get("fornecedor") or None, _num(linha.get("quantidade_pdf")),
        linha.get("status") or "", linha.get("destino") or "revisar",
        # CRU, não `estado_documento(...)`: a coluna guarda o veredito da extração
        # (`ok`/`fora_de_escopo`/`ilegivel`), e envolvê-la no enum da fila achatava `ok` e
        # `fora_de_escopo` em `extraido` (migration 0015).
        linha.get("doc_status") or "ilegivel", run_id,
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
        if extracao.get("hash_arquivo"):
            repo_doc.gravar_hash_arquivo(s, [(doc_ctrl, extracao["hash_arquivo"])])
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
                    f"({resolucao_extract.info.name}, pdf: {params.pdf_engine}), "
                    f"casamento: {resolucao_chat.info.name}, "
                    f"concorrência: {params.concurrency_docs} documentos")

    prompts_ativos = {}
    try:
        with db.session() as sessao:
            prompts_ativos = prompts_resolver.carregar_ativos(
                sessao, ["extrair_tabela_documento", "casar_itens_tabela"])
    except Exception:  # noqa: BLE001 — sem banco de prompts, cai no hardcoded
        prompts_ativos = {}

    # Um cliente por thread nos dois casos: compartilhar um cliente HTTP serializa as chamadas.
    _tls = threading.local()

    def extrator():
        if not hasattr(_tls, "e"):
            _tls.e = ctx.providers.novo_extract(curador_kwargs={
                "max_retries": 3, "prompts_ativos": prompts_ativos,
                # O anexo pode ter dezenas de MB e o documento inteiro passa pelo modelo: o
                # timeout padrão de 60s não cobre isso.
                "timeout": 600,
                "extra_body": {**_plugin_pdf(params.pdf_engine),
                               **_roteamento(params.provider_sort)}})
        return _tls.e

    def curador_factory():
        if not hasattr(_tls, "c"):
            _tls.c = ctx.providers.novo_chat(curador_kwargs={
                "max_retries": 6, "prompts_ativos": prompts_ativos,
                **sem_reasoning(resolucao_chat.info.name)}).curador
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
                          resolucao_extract.info.name, resolucao_extract.info)
        _contabilizar(doc_ctrl, extracao, run_id,
                      resolucao_extract.info, resolucao_chat.info)

        # A extração deu certo, mas o CASAMENTO pode ter quebrado. Os dois falham de formas
        # diferentes e precisam aparecer diferentes na tela — misturá-los foi o que escondeu
        # a falha em massa da etapa 3 em 2026-08-25.
        erro_casamento = extracao.get("_erro_casamento")
        if erro_casamento is not None:
            # UM erro por documento, não um por item. A unidade da etapa é o documento (é o
            # que a barra de progresso conta), e uma chamada de casamento que falha produz um
            # erro — não N. Em 2026-08-29 a tela mostrou "erros: 20" para UM documento com 20
            # candidatos, e a leitura natural foi "20 documentos falharam".
            ctx.item_error(doc_ctrl, erro_casamento, tipo="llm",
                           name=f"casamento de {len(linhas_item)} candidatos")
            ctx.log("aviso", f"[yellow][5] {doc_ctrl}: extraiu a tabela, mas o casamento "
                             f"falhou — {str(erro_casamento)[:160]}[/]")

        if not extracao.get("tabela_texto"):
            # Documento sem tabela não é falha de provedor — está gravado e não volta à fila.
            # Não conta como erro, mas também não conta como sucesso para o circuit breaker.
            ctx.log("aviso", f"[yellow][5] {doc_ctrl}: sem tabela de itens no documento[/]")
        elif erro_casamento is None:
            acumulador.registra_ok()

    def err_doc(doc_ctrl, exc):
        ctx.item_error(doc_ctrl, exc)
        motivo = regras.falha_permanente(exc)
        if motivo:
            # Sai da trilha de retentativa: grava como documento já tentado, do mesmo jeito
            # que um documento sem tabela. Sem isso ele fica em `descoberto` e volta em toda
            # execução para tomar o mesmo 400 — 282 documentos assim em 2026-08-30.
            linhas, extracao = _sem_tabela(grupos[doc_ctrl])
            _gravar_documento(doc_ctrl, linhas, extracao, run_id, model,
                              resolucao_extract.info.name, resolucao_extract.info)
            ctx.log("aviso", f"[yellow][5] {doc_ctrl}: {motivo} — não será tentado de novo. "
                             f"{str(exc)[:160]}[/]")
        else:
            ctx.log("aviso", f"[yellow][5] erro em {doc_ctrl}: {str(exc)[:160]}[/]")
        acumulador.registra_erro(exc, permanente=bool(motivo))

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
    # Os permanentes aparecem separados porque a reação a eles é outra: não adianta rodar de
    # novo, e o número deles é o que diz se vale trocar de `pdf_engine`.
    detalhe = (f" ({acumulador.permanentes} recusados pelo parser, não serão retentados)"
               if acumulador.permanentes else "")
    ctx.log("info", f"[bold {cor}][5] Concluído.[/] {acumulador.erros} erros{detalhe}. "
                    f"→ banco ({cont})")

    return StepResult(
        processed=acumulador.ok, errors=acumulador.erros,
        metrics={"documentos_pendentes": len(pend), "itens_nos_pendentes": n_itens, **cont},
    )
