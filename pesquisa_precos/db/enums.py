"""
Tipos enumerados do banco — espelho fiel de docs/02_SCHEMA.md §2.

Os `str, Enum` daqui existem para o Python; quem cria os tipos no PostgreSQL é a migration
inicial, com o DDL literal do documento. `NOMES` é a lista usada pelos modelos para referenciar
o tipo já existente (`create_type=False`), evitando que o SQLAlchemy tente recriá-lo.

Os VALORES são normativos: mudar um valor aqui sem migration correspondente faz o insert
falhar no banco, não no Python.
"""

from enum import StrEnum


class _Valor(StrEnum):
    """Enum de string que serializa como o próprio valor (é o que o psycopg manda ao PG)."""


class TipoCatalogo(_Valor):
    material = "material"
    servico = "servico"


class TipoDocumento(_Valor):
    contrato = "contrato"
    ata = "ata"


class EstadoDocumento(_Valor):
    descoberto = "descoberto"          # capa obtida da API, nada baixado
    fora_de_escopo = "fora_de_escopo"  # nenhum item sobreviveu ao corte da etapa 4
    baixando = "baixando"
    extraido = "extraido"              # texto obtido, PDF já descartado
    ilegivel = "ilegivel"              # o modelo não achou tabela de itens no documento
    suspeito = "suspeito"              # texto obtido mas nenhum item confirmou (PDF trocado)
    erro = "erro"


class DocStatus(_Valor):
    """O veredito da etapa 5 sobre um documento — não confundir com `EstadoDocumento`.

    `EstadoDocumento` é a FILA ("já foi processado?"); este é a QUALIDADE ("deu bom
    resultado?"). Até a migration 0015 a coluna `item_enriquecido.doc_status` usava o enum da
    fila, o que achatava `ok` e `fora_de_escopo` num único `extraido` e deixava a coluna
    respondendo sempre a mesma coisa em 2.545 documentos.

    `suspeito` não existe aqui: a etapa 5 deixou de produzi-lo em 2026-08-29.
    """

    ok = "ok"                          # ao menos um candidato casou com a tabela
    fora_de_escopo = "fora_de_escopo"  # a tabela saiu, mas nenhum candidato está nela
    ilegivel = "ilegivel"              # não saiu tabela do documento


class StatusEnriquecimento(_Valor):
    pdf_ok = "pdf_ok"
    pdf_ok_diverge = "pdf_ok_diverge"
    pdf_ok_preco_suspeito = "pdf_ok_preco_suspeito"
    pdf_ok_sem_preco = "pdf_ok_sem_preco"
    pdf_ok_sem_ref = "pdf_ok_sem_ref"
    qtd_nao_confere = "qtd_nao_confere"
    nao_encontrado = "nao_encontrado"
    sem_texto = "sem_texto"
    erro = "erro"


class DestinoItem(_Valor):
    manter = "manter"
    revisar = "revisar"
    descartar = "descartar"


class DecisaoRerank(_Valor):
    aceito = "aceito"
    ambiguo = "ambiguo"
    rejeitado = "rejeitado"


class VereditoPar(_Valor):
    sim = "sim"
    nao = "nao"
    indeterminado = "indeterminado"


class DecisaoFinalPar(_Valor):
    confirmado = "confirmado"
    rejeitado = "rejeitado"
    pendente = "pendente"


class ModoRun(_Valor):
    assisted = "assisted"
    sequential = "sequential"
    sample = "sample"
    simulation = "simulation"


class StatusRun(_Valor):
    open = "open"
    finished = "finished"
    aborted = "aborted"


class StatusEtapa(_Valor):
    not_started = "not_started"
    awaiting_approval = "awaiting_approval"
    running = "running"
    finished = "finished"
    outdated = "outdated"
    failed = "failed"
    cancelled = "cancelled"
    skipped = "skipped"


class AcaoExecucao(_Valor):
    update = "update"
    resume = "resume"
    redo = "redo"


class Capacidade(_Valor):
    chat = "chat"                # classificação e casamento — o modelo barato (ADR-004)
    embed = "embed"
    rerank = "rerank"
    # ADR-023: o modelo que recebe o PDF anexo e devolve a tabela de itens (etapa 5). Nasceu
    # no lugar de `pdf`, que era o serviço HTTP do companion — hoje é um LLM multimodal.
    extract = "extract"
    matching = "matching"        # BM25 + cosseno + corte em streaming, no serviço


# Nome do tipo no PostgreSQL → classe Python. A ordem é a de criação em 02_SCHEMA.md §2.
NOMES: dict[str, type[_Valor]] = {
    "tipo_catalogo": TipoCatalogo,
    "tipo_documento": TipoDocumento,
    "estado_documento": EstadoDocumento,
    "doc_status": DocStatus,
    "status_enriquecimento": StatusEnriquecimento,
    "destino_item": DestinoItem,
    "decisao_rerank": DecisaoRerank,
    "veredito_par": VereditoPar,
    "decisao_final_par": DecisaoFinalPar,
    "run_mode": ModoRun,
    "run_status": StatusRun,
    "step_status": StatusEtapa,
    "run_action": AcaoExecucao,
    "capability": Capacidade,
}
