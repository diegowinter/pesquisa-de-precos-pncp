"""
Reconstrução da URL pública do PNCP a partir do `numeroControlePNCP`.

Esta é a rede de segurança do ADR-012 ("PDF é efêmero; o texto extraído é o ativo"). O acervo
herdado guarda o caminho ABSOLUTO da pasta do PDF na máquina do usuário — 111 GB que ficaram no
repositório v2 e que o sistema novo não pode depender. Trocamos esse caminho por uma URL: se um
dia for preciso reprocessar um documento cujo texto não baste, dá para rebaixá-lo do PNCP a
partir do próprio número de controle. Sem esta função, a decisão de não migrar os PDFs ficaria
sem volta.

Formato do número de controle (é o que a API devolve, não uma convenção nossa):

    contrato: <cnpj14>-<tipo>-<seq>/<ano>              01664910000131-2-000068/2026
    ata:      <cnpj14>-<tipo>-<seqCompra>/<ano>-<seqAta>  00000368000150-1-000009/2026-000001

Os sequenciais vêm zero-padded no número de controle e SEM padding nas rotas do PNCP — daí o
`int()` em cada um. Zero à esquerda preservado geraria 404 silencioso.
"""

import re

# Página pública (front-end do PNCP). É a que serve para o operador auditar, e é a partir dela
# que a aba "Arquivos" leva ao PDF. A rota de API por arquivo já existe em
# `fetch_files.url_arquivos()`; aqui é a URL humana, que é o que a coluna guarda.
APP_BASE = "https://pncp.gov.br/app"

_CTRL = re.compile(r"^(\d{14})-(\d+)-0*(\d+)/(\d{4})(?:-0*(\d+))?$")


def partes_controle(numero_controle_pncp: str) -> dict | None:
    """Decompõe o número de controle, ou `None` se não casar com o formato conhecido."""
    m = _CTRL.match((numero_controle_pncp or "").strip())
    if not m:
        return None
    cnpj, tipo, seq, ano, seq_ata = m.groups()
    return {
        "cnpj": cnpj,
        "tipo": tipo,
        "sequential": int(seq),
        "ano": int(ano),
        "sequencial_ata": int(seq_ata) if seq_ata else None,
    }


def chave_compra(numero_controle_pncp: str) -> str:
    """A COMPRA a que o documento pertence — o prefixo até o ano, sem o sequencial da ata.

        ata:      00348003000110-1-000507/2025-000004  ->  00348003000110-1-000507/2025
        contrato: 01664910000131-2-000068/2026         ->  01664910000131-2-000068/2026

    É a chave de identidade do item (ADR-024). A API do PNCP entrega itens por COMPRA e não
    tem rota de itens por ata — confirmado na especificação OpenAPI: as rotas de ata são
    `atas`, `atas/{seq}`, `arquivos`, `contratos`, `partesenvolvidas` e `historico`, nenhuma
    de item. Enquanto o item nascia preso à ata, os 82 itens de um pregão da Embrapa viravam
    82 linhas em CADA uma das 25 atas dele — 8,4x de duplicação no acervo de atas.

    Para contrato o resultado coincide com o próprio documento. Isso NÃO é caso especial: é a
    mesma regra dando o mesmo valor, porque contrato não tem sufixo de ata. Verificado em
    1.661 de 1.661 contratos do acervo. Manter uma regra só é o ponto — quem chama nunca
    pergunta "é ata ou contrato?".

    Devolve "" quando o número de controle é irreconhecível, como `url_documento`: a etapa 2
    processa dezenas de milhares de documentos e um número malformado não pode derrubar o lote.

    ESTE é o único lugar que deriva a chave de compra. Um `regexp_replace` equivalente escrito
    à mão em SQL durante a investigação da ADR-024 perdeu o ano por um backslash comido pelo
    shell, e produziu contagens erradas sem levantar erro nenhum. `tests/test_estrutura.py`
    guarda essa exclusividade.
    """
    p = partes_controle(numero_controle_pncp)
    if not p:
        return ""
    # Reconstrói a partir das partes, sem padding — `partes_controle` já fez `int()` nos
    # sequenciais, e o número de controle traz zero à esquerda. Preservar o texto original do
    # prefixo é o que garante que a chave case com o que o PNCP devolve em
    # `numeroControlePncpCompra`.
    return numero_controle_pncp.strip().split("/")[0] + f"/{p['ano']}"


def url_documento(numero_controle_pncp: str, tipo_doc: str) -> str:
    """URL pública do contrato/ata, ou "" quando o número de controle é irreconhecível.

    Devolve string vazia em vez de levantar: a migração processa 68 mil documentos e um
    número malformado não pode derrubar o lote — o m07 conta quantos ficaram sem URL e
    reporta, que é o comportamento pedido em docs/05_MIGRACAO.md §4.
    """
    p = partes_controle(numero_controle_pncp)
    if not p:
        return ""
    if (tipo_doc or "").lower() == "contrato":
        return f"{APP_BASE}/contratos/{p['cnpj']}/{p['ano']}/{p['sequential']}"
    if p["sequencial_ata"] is None:
        # Ata sem sequencial próprio: o melhor alvo é a compra que a originou.
        return f"{APP_BASE}/editais/{p['cnpj']}/{p['ano']}/{p['sequential']}"
    return (f"{APP_BASE}/atas/{p['cnpj']}/{p['ano']}/"
            f"{p['sequential']}/{p['sequencial_ata']}")
