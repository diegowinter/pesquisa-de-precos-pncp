"""
Política de retenção — implementação da tabela de docs/02_SCHEMA.md §11.

Decidida na Fase 2 e não depois, porque `documento_pagina` cresce sem limite: 888 mil linhas e
2,6 GB de texto já hoje, e a step 5 acrescenta a cada coleta. Sem política, o banco vira o
novo problema dos 111 GB de PDF.

O que **não** é apagado, nunca, e o motivo (ADR-007 — reprocessar é perda):

    item, item_enriquecido, par, grupo_item     é o produto
    texto_classificacao, embedding_cache        active caro; recomprá-lo é perda de dinheiro
    label                                      base de calibração; append-only
    llm_call                                 série histórica de custo
    documento_extracao.itens_json               pequeno, e é o insumo da estratégia 'full'

Só duas coisas expiram:

    documento_pagina.texto   180 dias após o documento chegar a 'extraido'
    run_log                   90 dias

O texto de página é o único caso em que apagar é seguro, e mesmo assim só porque existe rede:
o documento guarda `url_pncp` + `hash_arquivo` + `n_paginas`, então dá para rebaixar do PNCP e
reextrair. Sem `url_pncp` preenchida, apagar seria irreversível — por isso `limpar_paginas()`
se recusa a tocar em documento sem URL.

Nada aqui roda sozinho. Todas as funções têm `simular=True` por padrão: devolvem o que
apagariam sem apagar. Quem dá o play é o operador (ADR-005).
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

DIAS_PAGINA = 180
DIAS_RUN_LOG = 90


@dataclass
class Prevista:
    """Resultado de uma passada de retenção. `bytes_texto` é 0 fora do caso das páginas."""

    alvo: str
    linhas: int
    bytes_texto: int = 0
    aplicado: bool = False

    def __str__(self) -> str:
        tam = f", {self.bytes_texto / 1e6:.0f} MB de texto" if self.bytes_texto else ""
        verbo = "apagadas" if self.aplicado else "apagáveis"
        return f"{self.alvo}: {self.linhas} linhas {verbo}{tam}"


def limpar_paginas(sessao: Session, *, dias: int = DIAS_PAGINA,
                   simular: bool = True) -> Prevista:
    """Páginas de documentos 'extraido' há mais de `dias` E que tenham `url_pncp`.

    A exigência de `url_pncp` não é zelo excessivo: sem ela, o texto apagado não é
    recuperável de lugar nenhum (o PDF já foi descartado logo após a extração, ADR-012).
    Documento sem URL simplesmente não expira — aparece como retido, não como erro.
    """
    where = """
        FROM documento_pagina p
        JOIN documento d USING (numero_controle_pncp)
       WHERE d.estado = 'extraido'
         AND d.url_pncp IS NOT NULL AND d.url_pncp <> ''
         AND d.updated_at < now() - make_interval(days => :dias)
    """
    linhas, bytes_texto = sessao.execute(
        text(f"SELECT count(*), COALESCE(sum(p.n_chars), 0) {where}"),
        {"dias": dias}).one()
    prev = Prevista("documento_pagina", int(linhas), int(bytes_texto))
    if simular or not linhas:
        return prev
    sessao.execute(text(f"""
        DELETE FROM documento_pagina dp
         USING (SELECT p.numero_controle_pncp, p.arquivo, p.pagina {where}) alvo
         WHERE dp.numero_controle_pncp = alvo.numero_controle_pncp
           AND dp.arquivo = alvo.arquivo AND dp.pagina = alvo.pagina
    """), {"dias": dias})
    prev.aplicado = True
    return prev


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
    return [limpar_paginas(sessao, simular=simular),
            limpar_run_log(sessao, simular=simular)]


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
