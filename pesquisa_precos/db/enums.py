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
    ilegivel = "ilegivel"              # nenhuma página produziu texto útil
    suspeito = "suspeito"              # texto obtido mas nenhum item confirmou (PDF trocado)
    erro = "erro"


class EstrategiaExtracao(_Valor):
    janela = "janela"
    completa = "completa"
    visao = "visao"


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
    assistido = "assistido"
    sequencial = "sequencial"
    amostra = "amostra"
    simulacao = "simulacao"


class StatusRun(_Valor):
    aberto = "aberto"
    concluido = "concluido"
    abortado = "abortado"


class StatusEtapa(_Valor):
    nao_iniciada = "nao_iniciada"
    aguardando_aprovacao = "aguardando_aprovacao"
    executando = "executando"
    concluida = "concluida"
    desatualizada = "desatualizada"
    falhou = "falhou"
    cancelada = "cancelada"
    pulada = "pulada"


class AcaoExecucao(_Valor):
    atualizar = "atualizar"
    retomar = "retomar"
    refazer = "refazer"


class Capacidade(_Valor):
    chat = "chat"
    embed = "embed"
    rerank = "rerank"
    ocr = "ocr"
    # Fase 11 (ADR-019): o processamento pesado que ainda rodava em processo vira serviço.
    pdf = "pdf"                  # baixa o PDF, extrai texto por página, chama o OCR por dentro
    pareamento = "pareamento"    # BM25 + cosseno + corte em streaming


# Nome do tipo no PostgreSQL → classe Python. A ordem é a de criação em 02_SCHEMA.md §2.
NOMES: dict[str, type[_Valor]] = {
    "tipo_catalogo": TipoCatalogo,
    "tipo_documento": TipoDocumento,
    "estado_documento": EstadoDocumento,
    "estrategia_extracao": EstrategiaExtracao,
    "status_enriquecimento": StatusEnriquecimento,
    "destino_item": DestinoItem,
    "decisao_rerank": DecisaoRerank,
    "veredito_par": VereditoPar,
    "decisao_final_par": DecisaoFinalPar,
    "modo_run": ModoRun,
    "status_run": StatusRun,
    "status_etapa": StatusEtapa,
    "acao_execucao": AcaoExecucao,
    "capacidade": Capacidade,
}
