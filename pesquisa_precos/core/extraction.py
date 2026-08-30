"""
Regras de negócio da etapa 5 — confirmação do item, veredito do documento e destino.

Estas regras são o que sobrou do pacote `strategies/` (aposentado na ADR-023): elas nunca
dependeram de COMO o texto chegou, só do par (item da API, item extraído do documento). O
caminho de extração mudou duas vezes — de quatro estratégias plugáveis para uma chamada com o
PDF anexo (ADR-023), e depois de um casamento por item para um por documento (ADR-024) — e só
`doc_status_de_motivos` precisou de ajuste, para ganhar `fora_de_escopo`. É a evidência de que
o corte está no lugar certo.

`validar_extracao` recebe o mesmo dict de sempre: {"descricao_completa", "preco_unitario",
"quantidade", "encontrado"}. Hoje quem o produz é `Curador.casar_itens_tabela`, que casa os
candidatos de uma compra contra a tabela que o modelo de extração leu do documento.
"""

# Match exato de preço (até os centavos) acima deste valor já é fingerprint único: confirma
# o item mesmo sem casar a quantidade (contratos de serviço têm qtd=1 e o doc não a reafirma).
PRECO_FINGERPRINT = 1000.0

# Banda de sanidade: o preço homologado costuma ficar em [0,3× … 3×] do estimado da API. Fora
# disso é quase sempre misparse de milhar/decimal (ou item errado) — mantém a descrição, mas
# marca o preço como suspeito em vez de aceitar cegamente.
BANDA_SANIDADE_MIN = 0.3
BANDA_SANIDADE_MAX = 3.0


def num(v) -> float | None:
    """Converte número BR ('1.234,56') ou US ('1234.56')/cru para float. None se não der."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if "," in s:  # vírgula = separador decimal (BR): pontos são milhar
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def validar_extracao(extraido: dict, item: dict) -> tuple[str, float | None, float | None]:
    """Confirma que a extração achou o item CERTO e devolve (status, preco_pdf, divergencia).

    O item é confirmado pela QUANTIDADE (fingerprint anti-colisão/PDF-trocado) OU por um match
    exato de preço alto. Confirmado o item, o PREÇO deixa de ser critério de aceite e vira
    SAÍDA: a API traz o valor estimado, o PDF o valor homologado/registrado (o que interessa).
    Divergência é sinalizada, não descartada (docs/08_CONVENCOES.md §5.9).

    Valores de `status` batem 1:1 com o enum `status_enriquecimento` de docs/02_SCHEMA.md §2:
      pdf_ok / pdf_ok_diverge / pdf_ok_preco_suspeito / pdf_ok_sem_preco / pdf_ok_sem_ref /
      qtd_nao_confere / nao_encontrado.
    """
    if not (extraido.get("encontrado") and extraido.get("descricao_completa")):
        return "nao_encontrado", None, None
    pe, pa = num(extraido.get("preco_unitario")), num(item.get("preco_unitario"))
    qe, qa = num(extraido.get("quantidade")), num(item.get("quantidade"))
    qtd_ok = qe is not None and qa is not None and abs(qe - qa) <= max(1.0, 0.01 * abs(qa))
    preco_ok = pe is not None and pa is not None and pa != 0 and abs(pe - pa) / abs(pa) <= 0.01
    confirmado = qtd_ok or (preco_ok and pa is not None and abs(pa) >= PRECO_FINGERPRINT)
    if not confirmado:
        return "qtd_nao_confere", None, None
    if pe is None or pe <= 0:
        return "pdf_ok_sem_preco", None, None
    if pa is None or pa == 0:
        return "pdf_ok_sem_ref", pe, None
    if preco_ok:
        return "pdf_ok", pe, 0.0
    div = round((pe - pa) / pa, 4)
    if pe < BANDA_SANIDADE_MIN * abs(pa) or pe > BANDA_SANIDADE_MAX * abs(pa):
        return "pdf_ok_preco_suspeito", pe, div
    return "pdf_ok_diverge", pe, div


def doc_status_de_motivos(status_por_item: dict[str, str]) -> str:
    """Veredito do documento INTEIRO, a partir do status de todos os seus candidatos.

        não saiu tabela do documento  -> `ilegivel`
        saiu tabela, nenhum casou     -> `fora_de_escopo`
        ao menos um casou             -> `ok`

    **`suspeito` deixou de ser produzido aqui** (2026-08-29). Ele era o detector de PDF
    trocado, inferido de "nenhum item confirmou" — inferência boa enquanto o item pertencia a
    UMA ata, e errada depois da ADR-024. Medido no acervo: a ata 000004 do pregão 507 tem os
    itens 1, 2 e 3 (mouse pad, key pad, apoio de pés), todos cortados na etapa 4; seus
    candidatos são coturno e bota, que estão em outras atas. O PDF está perfeito — o que falta
    é escopo, não integridade.

    O detector também vale menos agora: o item é procurado em TODAS as atas da compra, então
    uma ata com o arquivo errado não faz mais o item se perder, só a deixa sem contribuição.

    Distinguir de verdade exigiria mandar todos os itens da compra como candidatos (não só os
    sobreviventes) e ver se algum casa — ~4x mais tokens na chamada, para um sinal que já não
    protege contra perda. Fica registrado como o caminho, se um dia o detector voltar a valer.
    """
    motivos = list(status_por_item.values())
    if not motivos:
        return "fora_de_escopo"
    n_ok = sum(m.startswith("pdf_ok") for m in motivos)
    n_legivel = sum(m != "sem_texto" for m in motivos)
    if n_legivel == 0:
        return "ilegivel"
    if n_ok == 0:
        return "fora_de_escopo"
    return "ok"


def destino_de(status: str, doc_status: str) -> str:
    """manter (confirmado) / revisar (documento problemático) / descartar (não está aqui).

    `fora_de_escopo` cai em `descartar`, não em `revisar`: o item não estar nesta ata é a
    resposta CERTA, e mandar para revisão manual milhares de pares que sabemos serem
    inexistentes é ruído. `revisar` fica para `ilegivel` e `suspeito` — documento que pode
    estar escondendo item de verdade.
    """
    if status.startswith("pdf_ok"):
        return "manter"
    if doc_status in ("suspeito", "ilegivel"):
        return "revisar"
    return "descartar"


def estado_documento(doc_status: str | None) -> str:
    """`doc_status` da regra de negócio → rótulo do enum `estado_documento` do banco.

    **`estado` responde "o trabalho caro foi feito?", não "deu bom resultado?"** — é a chave de
    resumo da etapa 5 (`estado <> 'extraido'` é o que volta para a fila). Documento cuja tabela
    saiu está FEITO, mesmo que nenhum candidato tenha casado: reprocessá-lo baixaria o mesmo
    PDF e chamaria o mesmo modelo para chegar à mesma resposta. Só `ilegivel` volta, porque aí
    trocar de `pdf_engine` ou de modelo pode mudar o resultado.

    Antes de 2026-08-29 `fora_de_escopo` (então chamado de `suspeito`) voltava para a fila, e
    isso repagava a extração de milhares de documentos a cada execução.

    O veredito de qualidade não se perde: ele continua em `item_enriquecido.doc_status`.
    """
    return "ilegivel" if doc_status == "ilegivel" else "extraido"
