"""
Listagem dos passos da migração e o estado de cada um.

NÃO roda a migração. Deliberadamente: `pg_dump` antes de cada agregado é obrigatório
(docs/05_MIGRACAO.md §1.4), e um botão "migrar tudo" convida a pular exatamente isso. Cada
passo é disparado à mão, com o relatório lido entre um e outro — é o mesmo human-in-the-loop
que rege a pipeline (ADR-005).

Uso: python -m migracao
"""

from rich.table import Table

from pesquisa_precos.db import session as db
from migracao import PASSOS
from migracao._comum import Retomada, console

# Só os passos que leem CSV grande gravam retomada; os demais são rápidos o bastante para
# recomeçar do zero, e um checkpoint a mais seria estado para manter sem ganho.
CHECKPOINT_DO_PASSO = {
    "m07_documentos_itens": "m07_itens",
    "m08_classificacao": "m08_classificacao",
    "m09_sobreviventes": "m09_sobreviventes",
    "m10_texto_pdf": "m10_texto_pdf",
    "m11_enriquecidos": "m11_enriquecidos",
    "m13_rotulos": "m13_rotulos",
}


def main() -> None:
    ok, detalhe = db.is_available()
    console.print("[bold cyan]Migração CSV → PostgreSQL (Fase 2)[/]")
    console.print(f"  banco: {detalhe}" if ok else f"  [red]banco indisponível: {detalhe}[/]")

    tabela = Table()
    tabela.add_column("#")
    tabela.add_column("passo")
    tabela.add_column("o que faz")
    tabela.add_column("retomada", justify="right")
    for i, (modulo, descricao) in enumerate(PASSOS, 1):
        chave = CHECKPOINT_DO_PASSO.get(modulo)
        linhas = Retomada.carregar(chave).linhas if chave else 0
        marca = f"{linhas:,} linhas".replace(",", ".") if linhas else "—"
        tabela.add_row(str(i), modulo, descricao, marca)
    console.print(tabela)
    console.print("\nRode um por vez, com `pg_dump` entre os agregados:")
    console.print("  [dim]python -m migracao.m01_config_inicial[/]")
    console.print("  [dim]python -m migracao.validar[/]  (ao fim de cada agregado)")


if __name__ == "__main__":
    main()
