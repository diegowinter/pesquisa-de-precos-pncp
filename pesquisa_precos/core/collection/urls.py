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
        "sequencial": int(seq),
        "ano": int(ano),
        "sequencial_ata": int(seq_ata) if seq_ata else None,
    }


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
        return f"{APP_BASE}/contratos/{p['cnpj']}/{p['ano']}/{p['sequencial']}"
    if p["sequencial_ata"] is None:
        # Ata sem sequencial próprio: o melhor alvo é a compra que a originou.
        return f"{APP_BASE}/editais/{p['cnpj']}/{p['ano']}/{p['sequencial']}"
    return (f"{APP_BASE}/atas/{p['cnpj']}/{p['ano']}/"
            f"{p['sequencial']}/{p['sequencial_ata']}")
