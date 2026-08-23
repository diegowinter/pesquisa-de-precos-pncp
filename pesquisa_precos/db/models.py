"""
Modelos SQLAlchemy 2.x — espelho do DDL normativo de docs/02_SCHEMA.md.

Estes modelos NÃO são a fonte da verdade do schema: quem cria as tabelas é a migration
`0001_schema_inicial`, escrita com o DDL literal do documento (que é normativo até o name do
índice). Os modelos existem para os repositórios terem tipos, e por isso precisam bater com o
banco — `tests/test_schema_banco.py` compara os dois por reflexão e falha se divergirem.

Referências circulares (02_SCHEMA.md §13): `termo.config_version_id`, `documento.descoberto_no_run_id`,
`texto_classificacao.run_id` etc. apontam para tabelas criadas depois. No SQLAlchemy isso é
inofensivo (a FK é resolvida por name, tarde); na migration é resolvido pela ordem de criação
+ ALTER TABLE no fim.

Convenção: colunas de dinheiro são `Numeric(18,4)` e chegam ao Python como `Decimal` — nunca
`float` (docs/08_CONVENCOES.md §5.8).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    Numeric,
    REAL,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pesquisa_precos.db import enums


class Base(DeclarativeBase):
    pass


def _enum(name: str) -> ENUM:
    """Referência a um tipo ENUM que a migration já criou (`create_type=False`)."""
    return ENUM(enums.NOMES[name], name=name, create_type=False,
                values_callable=lambda e: [v.value for v in e])


def _agora() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ── Configuração, prompts e provedores (02_SCHEMA.md §10) ───────────────────────────

class ConfigVersao(Base):
    __tablename__ = "config_version"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    label: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _agora()
    notes: Mapped[str | None] = mapped_column(Text)


class ConfigValor(Base):
    __tablename__ = "config_value"
    config_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("config_version.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)


class Prompt(Base):
    __tablename__ = "prompt"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)
    capability: Mapped[str] = mapped_column(
        _enum("capability"), nullable=False, server_default="chat")


class PromptVersao(Base):
    __tablename__ = "prompt_version"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prompt_name: Mapped[str] = mapped_column(
        Text, ForeignKey("prompt.name", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _agora()
    notes: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("prompt_name", "version"),)


class NotificacaoDestinatario(Base):
    """Fase 9 (CRUD via interface web) — quem recebe notificação de step concluída/falhou/gate
    aguardando. Credencial do canal (API key do Resend) fica só no `.env` (ADR-006); esta
    tabela guarda apenas QUEM recebe."""

    __tablename__ = "notification_recipient"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = _agora()


class Provedor(Base):
    __tablename__ = "provider"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(_enum("capability")), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    # A chave de API mora aqui, CIFRADA (Fase 14, ADR-022 — ver `db/segredo.py`). O
    # criptograma é amarrado ao `name` do provedor pelo AAD, então copiá-lo de uma linha para
    # outra falha ao decifrar em vez de trocar de chave em silêncio. `last4` é o que a tela
    # exibe; a chave em claro nunca sobe para API ou HTML.
    api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    api_key_last4: Mapped[str | None] = mapped_column(Text)
    api_key_key_id: Mapped[str | None] = mapped_column(Text)
    api_key_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Herança pré-ADR-022: NOME da variável de ambiente, nunca a chave. Ainda lido pelo
    # resolver enquanto o bloco 4 da Fase 14 (seed + migração de conteúdo) não roda.
    api_key_ref: Mapped[str | None] = mapped_column(Text)
    default_model: Mapped[str | None] = mapped_column(Text)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="32")
    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    cost_in_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    cost_out_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    # Preço médio de UMA chamada, em USD (Fase 14). Só alimenta o `estimar()` das etapas;
    # o custo consumado real vem de `llm_call`. NULL = "não informado" → estimativa sem
    # custo, em vez de um número inventado. 0.0 é diferente: é o provedor local, que é grátis.
    cost_usd_per_call: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    allows_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false")
    updated_at: Mapped[datetime] = _agora()


class CapacidadeProvedor(Base):
    __tablename__ = "provider_capability"
    capability: Mapped[str] = mapped_column(_enum("capability"), primary_key=True)
    provider: Mapped[str] = mapped_column(
        Text, ForeignKey("provider.name"), nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    # Proibido em 'embed' — ADR-006. A regra é aplicada em código, não por constraint:
    # o banco não sabe que trocar de provedor de embedding corrompe o espaço vetorial.
    fallback: Mapped[str | None] = mapped_column(Text, ForeignKey("provider.name"))


class ProvedorStatus(Base):
    __tablename__ = "provider_status"
    provider: Mapped[str] = mapped_column(
        Text, ForeignKey("provider.name", ondelete="CASCADE"), primary_key=True)
    healthy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = _agora()


# ── Execução: runs, etapas, log e custo (02_SCHEMA.md §9) ───────────────────────────

class Run(Base):
    __tablename__ = "run"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    label: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(
        _enum("run_mode"), nullable=False, server_default="assisted")
    status: Mapped[str] = mapped_column(
        _enum("run_status"), nullable=False, server_default="open")
    config_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("config_version.id"), nullable=False)
    cost_cap_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0")
    document_limit: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _agora()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEtapa(Base):
    __tablename__ = "run_step"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("run.id", ondelete="CASCADE"), nullable=False)
    step: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        _enum("step_status"), nullable=False, server_default="not_started")
    action: Mapped[str | None] = mapped_column(_enum("run_action"))
    fingerprint: Mapped[str | None] = mapped_column(Text)
    effective_params: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default="{}")
    params_override: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default="{}")
    total: Mapped[int | None] = mapped_column(Integer)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pid: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0")
    metrics: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default="{}")
    error_message: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("run_id", "step"),)


class ExecucaoLock(Base):
    __tablename__ = "run_lock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default="1")
    run_etapa_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("run_step.id"))
    pid: Mapped[int | None] = mapped_column(Integer)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("id = 1", name="execucao_lock_id_check"),)


class RunLog(Base):
    __tablename__ = "run_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("run.id", ondelete="CASCADE"), nullable=False)
    step: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _agora()


class ErroItem(Base):
    __tablename__ = "item_error"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("run.id", ondelete="CASCADE"), nullable=False)
    step: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    error_type: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = _agora()


class LlmChamada(Base):
    __tablename__ = "llm_call"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("run.id", ondelete="CASCADE"))
    step: Mapped[str | None] = mapped_column(Text)
    capability: Mapped[str] = mapped_column(_enum("capability"), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("prompt_version.id"))
    key: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = _agora()


# ── Catálogo e termos (02_SCHEMA.md §3) ─────────────────────────────────────────────

class CatalogoRaw(Base):
    """CATMAT/CATSER **completo**, sem allow-list (ADR-017) — o universo de onde se cura.

    "raw" = completo, não "formato bruto da API": material e serviço chegam com nomes de campo
    diferentes (`descricaoItem`/`nomePdm` vs. `nomeServico`), e a normalização acontece na
    ingestão. É o que permite derivar `catalogo_item` por SQL puro.

    Sem esta tabela não há como escolher PDM pela interface — a tela precisa listar o que
    EXISTE, não só o que já foi curado.
    """

    __tablename__ = "catalogo_raw"
    tipo: Mapped[str] = mapped_column(_enum("tipo_catalogo"), primary_key=True)
    codigo: Mapped[str] = mapped_column(Text, primary_key=True)
    codigo_pdm: Mapped[str | None] = mapped_column(Text)
    nome_pdm: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    codigo_grupo: Mapped[str | None] = mapped_column(Text)
    nome_grupo: Mapped[str | None] = mapped_column(Text)
    nome_classe: Mapped[str | None] = mapped_column(Text)
    baixado_em: Mapped[datetime] = _agora()


class PdmPermitido(Base):
    """Allow-list curada (ADR-017) — o que era `PDMS_MATERIAIS`/`CODIGOS_SERVICOS` no código.

    `codigo` muda de significado com o tipo, herdando `catalogo.local.filtrar_curado()`:
    material → `codigoPdm`; servico → `codigoServico`. É por isso que a PK é composta e o
    join da derivação é diferente por tipo.

    `active = false` não é o mesmo que ausente: guarda a exclusão DELIBERADA com o motivo
    (`observacao`), para que a decisão não pareça esquecimento na próxima leitura.
    """

    __tablename__ = "pdm_permitido"
    tipo: Mapped[str] = mapped_column(_enum("tipo_catalogo"), primary_key=True)
    codigo: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    observacao: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _agora()
    updated_at: Mapped[datetime] = _agora()


class GrupoPermitido(Base):
    """Grupos de segurança pública (ADR-017) — o que era `GRUPOS_MATERIAIS`/`GRUPOS_SERVICOS`.

    Separada de `pdm_permitido` de propósito: esta define o RECORTE DO DOWNLOAD (quais
    `codigoGrupo` a 0a pagina com `--so-grupos-seguranca`), não o ESCOPO da pesquisa. Sem a
    flag, a etapa baixa o catálogo inteiro e esta tabela nem é consultada.
    """

    __tablename__ = "grupo_permitido"
    tipo: Mapped[str] = mapped_column(_enum("tipo_catalogo"), primary_key=True)
    codigo: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    observacao: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _agora()
    updated_at: Mapped[datetime] = _agora()


class CatalogoItem(Base):
    __tablename__ = "catalogo_item"
    # PK composta: o código só é único DENTRO do tipo (um CATMAT e um CATSER podem colidir).
    tipo: Mapped[str] = mapped_column(_enum("tipo_catalogo"), primary_key=True)
    codigo: Mapped[str] = mapped_column(Text, primary_key=True)
    codigo_pdm: Mapped[str | None] = mapped_column(Text)
    nome_pdm: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    codigo_grupo: Mapped[str | None] = mapped_column(Text)
    nome_grupo: Mapped[str | None] = mapped_column(Text)
    nome_classe: Mapped[str | None] = mapped_column(Text)
    categoria: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = _agora()
    updated_at: Mapped[datetime] = _agora()


class CatalogoDownload(Base):
    """Checkpoint de página do download da 0a — o que era `checkpoints/0a_parts_<tipo>/`.

    `prefixo` é `full` ou `g<codigoGrupo>`: o mode `--so-grupos-seguranca` pagina cada grupo
    separadamente, e sem o prefixo as páginas de dois grupos colidiriam na PK.
    """

    __tablename__ = "catalogo_download"
    tipo: Mapped[str] = mapped_column(_enum("tipo_catalogo"), primary_key=True)
    prefixo: Mapped[str] = mapped_column(Text, primary_key=True)
    pagina: Mapped[int] = mapped_column(Integer, primary_key=True)
    n_linhas: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    baixado_em: Mapped[datetime] = _agora()


class CatalogoSnapshot(Base):
    __tablename__ = "catalogo_snapshot"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    capturado_em: Mapped[datetime] = _agora()
    tipo: Mapped[str] = mapped_column(_enum("tipo_catalogo"), nullable=False)
    codigo: Mapped[str] = mapped_column(Text, nullable=False)
    hash_linha: Mapped[str] = mapped_column(Text, nullable=False)


class Termo(Base):
    __tablename__ = "termo"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    termo: Mapped[str] = mapped_column(Text, nullable=False)
    termo_norm: Mapped[str] = mapped_column(Text, nullable=False)  # chave de dedup
    categoria: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    excluido_por: Mapped[str | None] = mapped_column(Text)
    excluido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("config_version.id"))
    created_at: Mapped[datetime] = _agora()
    __table_args__ = (UniqueConstraint("termo_norm"),)


class TermoGeracao(Base):
    """Saída BRUTA do LLM por item do catálogo (step 1) — o que era `1_termos_item.csv`.

    Não é derivável de `termo`/`termo_codigo`: aquelas guardam o termo já expandido (variações
    de grafia + cópia sem acento) e já agregado por termo. `resolver_categorias()` usa o
    conjunto CRU como chave de desempate — com o expandido, a categoria de alguns códigos
    mudaria em silêncio.

    `categoria_llm` é a SUGESTÃO do modelo; a categoria final (pós-cascata) vive em
    `catalogo_item.categoria`. Guardar as duas permite recomputar a cascata sem rechamar o LLM.
    """

    __tablename__ = "termo_geracao"
    tipo: Mapped[str] = mapped_column(_enum("tipo_catalogo"), primary_key=True)
    codigo: Mapped[str] = mapped_column(Text, primary_key=True)
    termos: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    categoria_llm: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    prompt_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("prompt_version.id"))
    run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("run.id"))
    created_at: Mapped[datetime] = _agora()
    __table_args__ = (
        ForeignKeyConstraint(["tipo", "codigo"],
                             ["catalogo_item.tipo", "catalogo_item.codigo"],
                             ondelete="CASCADE"),
    )


class TermoCodigo(Base):
    __tablename__ = "termo_codigo"
    termo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("termo.id", ondelete="CASCADE"), primary_key=True)
    tipo: Mapped[str] = mapped_column(_enum("tipo_catalogo"), primary_key=True)
    codigo: Mapped[str] = mapped_column(Text, primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(["tipo", "codigo"],
                             ["catalogo_item.tipo", "catalogo_item.codigo"]),
    )


# ── Descoberta: documentos e itens (02_SCHEMA.md §4) ────────────────────────────────

class Documento(Base):
    __tablename__ = "documento"
    numero_controle_pncp: Mapped[str] = mapped_column(Text, primary_key=True)
    tipo_doc: Mapped[str] = mapped_column(_enum("tipo_documento"), nullable=False)
    orgao: Mapped[str | None] = mapped_column(Text)
    orgao_cnpj: Mapped[str | None] = mapped_column(Text)
    uf: Mapped[str | None] = mapped_column(Text)
    ano: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[date | None] = mapped_column(Date)                 # publicação (imutável)
    data_assinatura: Mapped[date | None] = mapped_column(Date)
    data_fim_vigencia: Mapped[date | None] = mapped_column(Date)
    # Campo REAL de ordenação da API do PNCP — é o watermark da coleta incremental.
    data_atualizacao_pncp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    url_pncp: Mapped[str | None] = mapped_column(Text)
    # Identificadores internos do PNCP (Fase 8, ADR-011/012): é com eles que a etapa 5 refaz
    # `listar_arquivos()` para baixar o PDF depois do corte, sem reconsultar a busca.
    numero_sequencial: Mapped[str | None] = mapped_column(Text)
    numero_sequencial_ata: Mapped[str | None] = mapped_column(Text)
    n_paginas: Mapped[int | None] = mapped_column(Integer)
    hash_arquivo: Mapped[str | None] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(
        _enum("estado_documento"), nullable=False, server_default="descoberto")
    n_itens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    n_itens_sobreviventes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0")
    descoberto_no_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("run.id"))
    created_at: Mapped[datetime] = _agora()
    updated_at: Mapped[datetime] = _agora()


class DocumentoTermo(Base):
    __tablename__ = "documento_termo"
    numero_controle_pncp: Mapped[str] = mapped_column(
        Text, ForeignKey("documento.numero_controle_pncp", ondelete="CASCADE"),
        primary_key=True)
    termo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("termo.id"), primary_key=True)


class Item(Base):
    __tablename__ = "item"
    item_key: Mapped[str] = mapped_column(Text, primary_key=True)
    numero_controle_pncp: Mapped[str] = mapped_column(
        Text, ForeignKey("documento.numero_controle_pncp", ondelete="CASCADE"), nullable=False)
    numero_item: Mapped[int] = mapped_column(Integer, nullable=False)
    descricao_api: Mapped[str] = mapped_column(Text, nullable=False)
    unidade: Mapped[str | None] = mapped_column(Text)
    quantidade: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    preco_unitario: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))  # homologado
    preco_estimado: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    fornecedor: Mapped[str | None] = mapped_column(Text)
    data_resultado: Mapped[date | None] = mapped_column(Date)
    # Calculado NA INGESTÃO (core.text.texto_hash), nunca na hora de classificar.
    texto_hash: Mapped[str] = mapped_column(Text, nullable=False)
    sobrevivente: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = _agora()
    __table_args__ = (UniqueConstraint("numero_controle_pncp", "numero_item"),)


class ColetaProgresso(Base):
    """(termo, tipo_doc) já varridos — o que era `checkpoints/2_progresso.csv`.

    NÃO é derivável do resultado, ao contrário dos checkpoints das outras etapas: uma busca
    legítima pode não trazer documento nenhum, e derivar de `documento` faria a etapa revarrer
    esses termos para sempre. Chaveado por `termo_id` porque o texto do termo pode ser
    reescrito pela curadoria; o id, não.
    """

    __tablename__ = "coleta_progresso"
    termo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("termo.id", ondelete="CASCADE"), primary_key=True)
    tipo_doc: Mapped[str] = mapped_column(_enum("tipo_documento"), primary_key=True)
    n_documentos: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    n_itens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    finished_at: Mapped[datetime] = _agora()


class ColetaPendente(Base):
    """Documento visto na busca mas ainda SEM resultado homologado — o que era
    `checkpoints/2_pendentes.csv`. O `--atualizar` revisita esta lista antes de tudo.

    `base` é o dict que `collect_pncp.revisitar_pendente()` consome de volta; guardá-lo inteiro
    em jsonb evita reimplementar o parser da API por fora dele.
    """

    __tablename__ = "coleta_pendente"
    numero_controle_pncp: Mapped[str] = mapped_column(Text, primary_key=True)
    tipo_doc: Mapped[str] = mapped_column(_enum("tipo_documento"), nullable=False)
    termo_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("termo.id", ondelete="SET NULL"))
    motivo: Mapped[str] = mapped_column(Text, nullable=False,
                                        server_default="sem_homologado")
    data: Mapped[str | None] = mapped_column(Text)
    base: Mapped[dict] = mapped_column(JSONB, nullable=False)
    visto_em: Mapped[datetime] = _agora()


class ColetaWatermark(Base):
    __tablename__ = "collection_watermark"
    termo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("termo.id", ondelete="CASCADE"), primary_key=True)
    tipo_doc: Mapped[str] = mapped_column(_enum("tipo_documento"), primary_key=True)
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = _agora()


# ── Classificação (02_SCHEMA.md §5) ─────────────────────────────────────────────────

class TextoClassificacao(Base):
    """Cache de classificação POR TEXTO — o ativo caro (uma chamada de LLM por linha).

    Sobrevive entre runs: o dedup deixa de ser intra-execução e vira permanente (ADR-007).
    """

    __tablename__ = "texto_classificacao"
    texto_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    unidade: Mapped[str | None] = mapped_column(Text)
    categorias: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}")
    confianca: Mapped[float | None] = mapped_column(REAL)
    prompt_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("prompt_version.id"))
    model: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("run.id"))
    created_at: Mapped[datetime] = _agora()


class ItemCategoria(Base):
    """Multi-label explodido por item. DERIVADA e barata — recomputável por SQL puro."""

    __tablename__ = "item_categoria"
    item_key: Mapped[str] = mapped_column(
        Text, ForeignKey("item.item_key", ondelete="CASCADE"), primary_key=True)
    categoria: Mapped[str] = mapped_column(Text, primary_key=True)


# ── Extração (02_SCHEMA.md §6) ──────────────────────────────────────────────────────

class DocumentoExtracao(Base):
    __tablename__ = "documento_extracao"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    numero_controle_pncp: Mapped[str] = mapped_column(
        Text, ForeignKey("documento.numero_controle_pncp", ondelete="CASCADE"), nullable=False)
    estrategia: Mapped[str] = mapped_column(_enum("extraction_strategy"), nullable=False)
    itens_json: Mapped[Any | None] = mapped_column(JSONB)
    n_paginas: Mapped[int | None] = mapped_column(Integer)
    n_paginas_ocr: Mapped[int | None] = mapped_column(Integer)
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("run.id"))
    created_at: Mapped[datetime] = _agora()
    __table_args__ = (UniqueConstraint("numero_controle_pncp", "estrategia"),)


class DocumentoPagina(Base):
    """O volume pesado (888k linhas, 2,6 GB de texto). Ver política de retenção em §11."""

    __tablename__ = "documento_pagina"
    numero_controle_pncp: Mapped[str] = mapped_column(
        Text, ForeignKey("documento.numero_controle_pncp", ondelete="CASCADE"),
        primary_key=True)
    arquivo: Mapped[str] = mapped_column(Text, primary_key=True)
    pagina: Mapped[int] = mapped_column(Integer, primary_key=True)
    fonte: Mapped[str] = mapped_column(Text, nullable=False)  # 'nativo' | 'ocr'
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    n_chars: Mapped[int] = mapped_column(Integer, Computed("length(texto)", persisted=True))


class ItemEnriquecido(Base):
    """CONTRATO DE SAÍDA DA ETAPA 5 — estável, independente de estratégia.

    As etapas 6, 7 e 8 leem SÓ esta tabela e ignoram a coluna `estrategia` (ADR-010).
    """

    __tablename__ = "item_enriquecido"
    item_key: Mapped[str] = mapped_column(
        Text, ForeignKey("item.item_key", ondelete="CASCADE"), primary_key=True)
    descricao_final: Mapped[str] = mapped_column(Text, nullable=False)
    fonte_descricao: Mapped[str] = mapped_column(Text, nullable=False)  # 'pdf' | 'api'
    preco_api: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    preco_pdf: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    divergencia_preco: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    fornecedor: Mapped[str | None] = mapped_column(Text)
    quantidade_pdf: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    status: Mapped[str] = mapped_column(_enum("status_enriquecimento"), nullable=False)
    destino: Mapped[str] = mapped_column(_enum("destino_item"), nullable=False)
    estrategia: Mapped[str] = mapped_column(_enum("extraction_strategy"), nullable=False)
    doc_status: Mapped[str] = mapped_column(_enum("estado_documento"), nullable=False)
    run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("run.id"))
    created_at: Mapped[datetime] = _agora()


# ── Pareamento (02_SCHEMA.md §7) ────────────────────────────────────────────────────

class Par(Base):
    """Uma tabela para 6a+6b+6c (ADR-013): a cardinalidade é 1:1:1 e o PK já é o mesmo."""

    __tablename__ = "par"
    par_key: Mapped[str] = mapped_column(Text, primary_key=True)
    tipo: Mapped[str] = mapped_column(_enum("tipo_catalogo"), nullable=False)
    codigo: Mapped[str] = mapped_column(Text, nullable=False)
    item_key: Mapped[str] = mapped_column(
        Text, ForeignKey("item.item_key", ondelete="CASCADE"), nullable=False)
    categoria: Mapped[str] = mapped_column(Text, nullable=False)
    # 6a
    score_bm25: Mapped[float | None] = mapped_column(REAL)
    score_cosseno: Mapped[float | None] = mapped_column(REAL)
    sobreviveu: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # 6b
    score_rerank: Mapped[float | None] = mapped_column(REAL)
    decisao: Mapped[str | None] = mapped_column(_enum("decisao_rerank"))
    # 6c (só a faixa ambígua chega aqui)
    veredito: Mapped[str | None] = mapped_column(_enum("veredito_par"))
    justificativa: Mapped[str | None] = mapped_column(Text)
    modelo_6c: Mapped[str | None] = mapped_column(Text)
    # Derivada: confirmado = (decisao='aceito') OU (veredito='sim'). Recomputada ao fim da 6c.
    final_decision: Mapped[str] = mapped_column(
        _enum("decisao_final_par"), nullable=False, server_default="pendente")
    run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("run.id"))
    updated_at: Mapped[datetime] = _agora()
    __table_args__ = (
        ForeignKeyConstraint(["tipo", "codigo"],
                             ["catalogo_item.tipo", "catalogo_item.codigo"]),
    )


class Rotulo(Base):
    """Append-only, base de calibração de threshold e futuro fine-tune. NUNCA truncar."""

    __tablename__ = "label"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    par_key: Mapped[str] = mapped_column(Text, nullable=False)
    texto_catalogo: Mapped[str] = mapped_column(Text, nullable=False)
    texto_item: Mapped[str] = mapped_column(Text, nullable=False)
    score_rerank: Mapped[float | None] = mapped_column(REAL)
    final_decision: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("run.id"))
    created_at: Mapped[datetime] = _agora()
    __table_args__ = (UniqueConstraint("par_key", "source"),)


class EmbeddingCache(Base):
    """A key INCLUI provider+model+dimensão (ADR-006 §1) — sem isso, trocar de provider
    mistura espaços vetoriais em silêncio. `vector` é float16 little-endian em bytea."""

    __tablename__ = "embedding_cache"
    texto_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    dimension: Mapped[int] = mapped_column(Integer, primary_key=True)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _agora()


# ── Resultado (02_SCHEMA.md §8) ─────────────────────────────────────────────────────

class GrupoItem(Base):
    __tablename__ = "grupo_item"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tipo: Mapped[str] = mapped_column(_enum("tipo_catalogo"), nullable=False)
    codigo: Mapped[str] = mapped_column(Text, nullable=False)
    item_key: Mapped[str] = mapped_column(
        Text, ForeignKey("item.item_key", ondelete="CASCADE"), nullable=False)
    par_key: Mapped[str] = mapped_column(Text, nullable=False)
    posicao: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 = mais barato
    preco_unitario: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    flag_preco: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    motivo_flag: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("run.id"), nullable=False)
    created_at: Mapped[datetime] = _agora()
    __table_args__ = (UniqueConstraint("run_id", "tipo", "codigo", "item_key"),)


class FaixaPreco(Base):
    __tablename__ = "faixa_preco"
    categoria: Mapped[str] = mapped_column(Text, primary_key=True)
    preco_min: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    preco_max: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    config_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("config_version.id"))


class Export(Base):
    __tablename__ = "export"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("run.id"), nullable=False)
    tipo: Mapped[str] = mapped_column(Text, nullable=False)     # 'completo' | 'novos'
    # ADR-018 §2: o XLSX vive no banco. `arquivo` (caminho relativo) sobrevive nullable só
    # pelas linhas geradas antes da Fase 10 — export novo preenche `conteudo`+`nome_arquivo`.
    arquivo: Mapped[str | None] = mapped_column(Text)
    nome_arquivo: Mapped[str | None] = mapped_column(Text)
    conteudo: Mapped[bytes | None] = mapped_column(LargeBinary)
    n_linhas: Mapped[int] = mapped_column(Integer, nullable=False)
    n_codigos: Mapped[int] = mapped_column(Integer, nullable=False)
    hash_arquivo: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _agora()


class ExportSnapshot(Base):
    """Baseline do `--novos`. ARMADILHA: sem snapshot prévio, a 1ª execução marca TUDO como
    novo — a correção é semear a partir do último export oficial (m16), não tratar como bug."""

    __tablename__ = "export_snapshot"
    tipo: Mapped[str] = mapped_column(_enum("tipo_catalogo"), primary_key=True)
    codigo: Mapped[str] = mapped_column(Text, primary_key=True)
    numero_controle_pncp: Mapped[str] = mapped_column(Text, primary_key=True)
    numero_item: Mapped[int] = mapped_column(Integer, primary_key=True)
    export_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("export.id"))
