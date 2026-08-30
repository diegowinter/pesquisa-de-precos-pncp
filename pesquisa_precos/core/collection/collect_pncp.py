"""
Lógica de coleta do PNCP como funções puras (etapa 2). Sem `rich`/`Progress`: quem orquestra
e mostra progresso é `steps/e2_collect.py`; aqui ficam busca, filtro de homologados e a
explosão em itens.

Regras de negócio (o download saiu daqui — ver ADR-011):
  1. Documento precisa ter arquivo do tipo alvo (Contrato / Ata de Registro de Preço).
  2. Precisa ter pelo menos um item Homologado.
  3. Passando as duas: explode 1 linha por item — SEM baixar PDF.

Fase 8 (docs/04_FASES.md, ADR-011): esta etapa deixou de baixar PDF. Antes disso, ~90% dos
documentos descobertos aqui eram baixados e boa parte descartada nas etapas 3/4 sem nunca
render item confirmado — banda, disco e CPU gastos à toa. Agora só a "capa" (metadados +
itens homologados da API) é obtida; o download vira responsabilidade da etapa 5, DEPOIS do
corte, só para os documentos que sobrevivem. `numero_sequencial`/`numero_sequencial_ata`
(mais `orgao_cnpj`/`ano`/`tipo_doc` já presentes) são o que a etapa 5 precisa para refazer
`fetch_files.listar_arquivos()` sem reconsultar a busca. `url_pncp`
(`core.collection.urls.url_documento`) é a rede de segurança do ADR-012 para rebaixar sob demanda.
"""



import threading

from pesquisa_precos.core.collection import search_pncp, fetch_files, fetch_items, urls

# Fontes de documento suportadas (tipo_doc na saída da etapa 2).
FONTES = ["contrato", "ata"]

# Colunas da saída da etapa 2. `item_key` é a chave universal do item daqui pra frente:
# CHAVE DA COMPRA + "::" + numeroItem (ADR-024). Era o número de controle do DOCUMENTO até
# 2026-08-29 — e como a API do PNCP só entrega itens por compra, isso copiava a lista inteira
# em cada ata da mesma compra (8,4x de duplicação no acervo de atas).
COLUNAS_ITENS = [
    "item_key", "compra_key", "tipo_doc", "numeroControlePNCP", "numeroItem", "descricao_api",
    "unidade", "quantidade", "preco_unitario", "orgao", "uf", "data",
    "conceitos_origem",
    # metadados extras úteis à exportação final (etapa 8):
    "ano", "orgao_cnpj", "data_fim_vigencia", "data_assinatura",
    # preço: preco_unitario = HOMOLOGADO (adjudicado); preco_estimado preserva o do edital.
    "preco_estimado", "fornecedor", "data_resultado",
    # Fase 8 (ADR-011/ADR-012): identificadores p/ a etapa 5 baixar o PDF DEPOIS do corte,
    # sem reconsultar a busca, e URL pública p/ rebaixar sob demanda (PDF nunca é persistido).
    "numero_sequencial", "numero_sequencial_ata", "url_pncp",
]


# ── Itens são da COMPRA, não do documento (ADR-024) ─────────────────────────────────
#
# Um pregão gera N atas, e a API do PNCP só tem itens por compra. Sem este cache, coletar as
# 25 atas do pregão 507 da Embrapa refazia 25 vezes o `fetch_itens()` (88 itens) E os 88
# `fetch_resultado_vencedor()` — mais de 2.200 chamadas à API para obter o mesmo dado. Com o
# cache, uma vez.
#
# A etapa 2 processa documentos em paralelo (`params.concurrency`), então o dicionário é
# protegido por lock. O teto existe porque uma execução longa passa por milhares de compras e
# a lista de itens de cada uma não é pequena; ao estourar, o cache é esvaziado inteiro — não
# há política de descarte fina porque as atas da mesma compra chegam próximas na busca, que
# é justamente quando o cache serve.
_TETO_CACHE_COMPRAS = 500
_cache_itens: dict[tuple, list[dict]] = {}
_lock_cache = threading.Lock()


def _itens_da_compra(cnpj: str, ano, seq_itens) -> list[dict]:
    """Itens HOMOLOGADOS da compra, com o resultado vencedor já resolvido. Memoizado."""
    chave = (str(cnpj), str(ano), str(seq_itens))
    with _lock_cache:
        cacheado = _cache_itens.get(chave)
    if cacheado is not None:
        return cacheado

    itens = fetch_items.fetch_itens(cnpj, ano, seq_itens, silent=True)
    homologados = fetch_items.filtrar_homologados(itens)
    enriquecidos = []
    for item in homologados:
        numero_item = item.get("numeroItem")
        estimado = item.get("valorUnitarioEstimado")
        # Preço real = valorUnitarioHomologado (adjudicado no /resultados). O estimado do
        # edital costuma ser placeholder (0/0,01); só cai nele quando não há resultado.
        forn = data_res = ""
        preco = estimado
        if item.get("temResultado"):
            res = fetch_items.fetch_resultado_vencedor(cnpj, ano, seq_itens, numero_item,
                                                       silent=True)
            if res and res.get("valorUnitarioHomologado") not in (None, "", 0):
                preco = res.get("valorUnitarioHomologado")
                forn = res.get("nomeRazaoSocialFornecedor") or ""
                data_res = res.get("dataResultado") or ""
        enriquecidos.append({**item, "_preco": preco, "_fornecedor": forn,
                             "_data_resultado": data_res, "_estimado": estimado})

    with _lock_cache:
        if len(_cache_itens) >= _TETO_CACHE_COMPRAS:
            _cache_itens.clear()
        _cache_itens[chave] = enriquecidos
    return enriquecidos


def limpar_cache_itens() -> None:
    """Zera o cache de itens por compra. A etapa 2 chama no início de cada execução — dado do
    PNCP muda entre execuções, e cache que sobrevive a um `run` esconderia atualização."""
    with _lock_cache:
        _cache_itens.clear()


def montar_item_key(compra_key: str, numero_item) -> str:
    """Chave do item. O primeiro componente é a COMPRA, nunca o documento (ADR-024).

    Quem tem o número de controle de um documento em mãos passa por `urls.chave_compra()`
    antes — é a única função que deriva essa chave.
    """
    return f"{compra_key}::{numero_item}"


def _base_resultado(r: dict, fonte: str) -> dict:
    """Extrai da resposta de busca os identificadores internos + metadados do documento."""
    if fonte == "contrato":
        numero_sequencial = r.get("numero_sequencial")
        seq_ata = None
    else:
        numero_sequencial = r.get("numero_sequencial_compra_ata")
        seq_ata = r.get("numero_sequencial")  # sequencial próprio da ata

    return {
        "tipo_doc": fonte,
        "ano": r.get("ano"),
        "numero_sequencial": numero_sequencial,
        "orgao_cnpj": r.get("orgao_cnpj"),
        "orgao": r.get("orgao_nome"),
        "uf": r.get("unidade_federativa_sigla") or r.get("uf") or "",
        "objetoCompra": r.get("description"),
        "data": r.get("data_publicacao_pncp") or r.get("data_assinatura") or "",
        "data_assinatura": r.get("data_assinatura"),
        "data_fim_vigencia": r.get("data_fim_vigencia"),
        "numeroControlePNCP": r.get("numero_controle_pncp"),
        "_seq_ata": seq_ata,
    }


def identificar(r: dict, fonte: str) -> dict:
    """Metadados normalizados do documento a partir de um resultado de busca (público).

    Usado pela etapa 2 para registrar documentos 'sem_homologado' como pendentes: o dict
    devolvido é persistido e depois repassado a `revisitar_pendente` numa rodada futura.
    """
    return _base_resultado(r, fonte)


def coletar_documento(r: dict, fonte: str, conceito: str) -> tuple[list[dict], str]:
    """
    Aplica as regras de negócio a um resultado de busca e devolve (linhas_de_item, status).

    status ∈ {ok, sem_identificacao, sem_arquivo, sem_homologado, erro}. `conceito` é o
    conceito da etapa 1 que trouxe este documento (vai para `conceitos_origem`). NÃO baixa
    PDF (Fase 8/ADR-011) — só confirma que existe arquivo do tipo alvo e que há item
    homologado; o download fica para a etapa 5, depois do corte.
    """
    return _coletar_de_base(_base_resultado(r, fonte), fonte, conceito)


def revisitar_pendente(base: dict, fonte: str, conceito: str) -> tuple[list[dict], str]:
    """Re-tenta coletar um documento antes 'sem_homologado', partindo do `base` persistido.

    Igual a `coletar_documento`, mas sem passar pela busca — consulta direto os itens do
    documento (endpoint de resultados). Se a homologação já saiu, devolve as linhas e status
    'ok'; senão, 'sem_homologado' de novo (segue pendente).
    """
    return _coletar_de_base(base, fonte, conceito)


def _coletar_de_base(base: dict, fonte: str, conceito: str) -> tuple[list[dict], str]:
    """Núcleo compartilhado por `coletar_documento` e `revisitar_pendente` (ver docstrings)."""
    cnpj, ano, seq = base["orgao_cnpj"], base["ano"], base["numero_sequencial"]
    seq_ata = base.get("_seq_ata")

    if not all([cnpj, ano, seq]) or (fonte == "ata" and not seq_ata):
        return [], "sem_identificacao"

    try:
        arquivos = fetch_files.listar_arquivos(fonte, cnpj, ano, seq, seq_ata, silent=True)
    except Exception:  # noqa: BLE001
        return [], "erro"
    alvos = fetch_files.selecionar_do_tipo(arquivos, fonte)
    if not alvos:
        return [], "sem_arquivo"

    if fonte == "contrato":
        seq_itens = fetch_items.resolver_sequencial_compra_contrato(cnpj, ano, seq, silent=True)
        if not seq_itens:
            return [], "sem_homologado"
    else:
        seq_itens = seq

    try:
        homologados = _itens_da_compra(cnpj, ano, seq_itens)
    except Exception:  # noqa: BLE001
        return [], "erro"
    if not homologados:
        return [], "sem_homologado"

    url_pncp = urls.url_documento(base["numeroControlePNCP"], fonte)
    # A identidade do item é a COMPRA (ADR-024). `numeroControlePNCP` continua na linha porque
    # a etapa 2 precisa dele para gravar o DOCUMENTO; ele já não entra no `item_key`.
    compra_key = urls.chave_compra(base["numeroControlePNCP"])

    linhas = []
    for item in homologados:
        # Preço e fornecedor já vêm resolvidos de `_itens_da_compra` — resolvê-los aqui
        # significaria uma chamada de `/resultados` por item POR ATA (ver ADR-024).
        numero_item = item.get("numeroItem")
        estimado = item.get("_estimado")
        preco = item.get("_preco")
        forn = item.get("_fornecedor") or ""
        data_res = item.get("_data_resultado") or ""
        linhas.append({
            "item_key": montar_item_key(compra_key, numero_item),
            "compra_key": compra_key,
            "tipo_doc": fonte,
            "numeroControlePNCP": base["numeroControlePNCP"],
            "numeroItem": numero_item,
            "descricao_api": item.get("descricao") or "",
            "unidade": item.get("unidadeMedida") or item.get("unidade") or "",
            "quantidade": item.get("quantidade"),
            "preco_unitario": preco,
            "orgao": base["orgao"] or "",
            "uf": base["uf"],
            "data": base["data"],
            "conceitos_origem": conceito,
            "ano": base["ano"],
            "orgao_cnpj": base["orgao_cnpj"],
            "data_fim_vigencia": base["data_fim_vigencia"] or "",
            "data_assinatura": base["data_assinatura"] or "",
            "preco_estimado": estimado,
            "fornecedor": forn,
            "data_resultado": data_res,
            "numero_sequencial": seq,
            "numero_sequencial_ata": seq_ata or "",
            "url_pncp": url_pncp,
        })
    return linhas, "ok"


def iter_resultados(termo: str, fonte: str, tam_pagina: int = search_pncp.TAM_PAGINA_DEFAULT, on_total=None):
    """Repassa o generator de busca do PNCP (paginação/retry ficam em search_pncp)."""
    yield from search_pncp.iter_resultados(termo, fonte, tam_pagina, on_total=on_total)
