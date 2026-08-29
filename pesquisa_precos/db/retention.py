"""
Política de retenção (docs/02_SCHEMA.md §11).

Uma coisa expira: o `run_log`, em 90 dias. Nada mais — item, par, grupo, classificação,
embedding, rótulo e histórico de custo são o produto ou ativo caro, e ficam (ADR-007).

O texto de página saiu daqui junto com a tabela `documento_pagina` (ADR-023): a etapa 5 não
transcreve mais o documento inteiro, e o que ela grava hoje é só a tabela de itens — pequena,
e o insumo direto do enriquecimento, não algo a expirar.

Nada roda sozinho: todas as funções têm `simular=True` por padrão e devolvem o que apagariam.
Ainda não há tela que as chame — quem quiser rodar, chama daqui.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

DIAS_RUN_LOG = 90


@dataclass
class Prevista:
    """Resultado de uma passada de retenção."""

    alvo: str
    linhas: int
    aplicado: bool = False

    def __str__(self) -> str:
        verbo = "apagadas" if self.aplicado else "apagáveis"
        return f"{self.alvo}: {self.linhas} linhas {verbo}"


def limpar_run_log(sessao: Session, *, dias: int = DIAS_RUN_LOG,
                   simular: bool = True) -> Prevista:
    """Log é diagnóstico, não produto. 90 dias cobrem qualquer investigação realista."""
    linhas = sessao.execute(
        text("SELECT count(*) FROM run_log WHERE created_at < now() - make_interval(days => :d)"),
        {"d": dias}).scalar_one()
    prev = Prevista("run_log", int(linhas))
    if simular or not linhas:
        return prev
    sessao.execute(
        text("DELETE FROM run_log WHERE created_at < now() - make_interval(days => :d)"),
        {"d": dias})
    prev.aplicado = True
    return prev


def aplicar(sessao: Session, *, simular: bool = True) -> list[Prevista]:
    """Passada completa. `simular=True` (padrão) é relatório, não ação."""
    return [limpar_run_log(sessao, simular=simular)]


def resumo_de_espaco(sessao: Session) -> list[tuple[str, str]]:
    """(tabela, tamanho legível) das maiores tabelas — o número que motiva a política."""
    return [
        (t, s) for t, s in sessao.execute(text("""
            SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid))
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
             ORDER BY pg_total_relation_size(c.oid) DESC
             LIMIT 12
        """)).all()
    ]
