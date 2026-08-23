"""
m03 — Run sintético "acervo migrado v2/v3".

Todo resultado carrega o `run_id` de source (ADR-015), e `grupo_item.run_id` é `NOT NULL`. O
acervo herdado, porém, não veio de nenhum run: veio de dezenas de execuções manuais de script
ao longo de meses, sem registro. Inventar um run por execução seria fabricar histórico que não
existe; deixar `run_id` nulo quebraria a rastreabilidade que o schema promete.

A saída honesta é UM run, marcado `concluido`, cujo rótulo diz exatamente o que ele é. Quem
consultar o banco depois lê "acervo migrado v2/v3" e sabe que aquilo não é uma execução
observável — não há progresso, custo nem log para ver, porque nunca houve.

Uso: python -m migracao.m03_run_historico
"""

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import execution as repo
from migracao._comum import Relatorio, cabecalho, console

ROTULO_CONFIG = "migrada do .env"


def migrar() -> Relatorio:
    rel = Relatorio("m03 — run do acervo migrado")
    with db.session() as s:
        existente = repo.run_do_acervo_migrado(s)
        if existente:
            rel.mais("run reaproveitado")
            rel.aviso(f"run #{existente} já existia — nada a fazer (idempotente).")
            return rel

        cv = repo.config_versao_por_rotulo(s, ROTULO_CONFIG)
        if cv is None:
            raise SystemExit(
                f"config_version {ROTULO_CONFIG!r} não existe. Rode `python -m "
                f"migracao.m01_config_inicial` antes — o run precisa apontar para uma config.")

        run_id = repo.criar_run(
            s, repo.ROTULO_ACERVO_MIGRADO, cv,
            mode="sequential", status="finished", created_by="migracao")
        rel.mais("run criado")
        rel.aviso(f"run #{run_id} — âncora de run_id de todo o acervo herdado.")
    return rel


def main() -> None:
    cabecalho("m03 — run histórico", [], "run")
    console.print(f"  banco  : {db.database_url()}")
    migrar().imprimir()


if __name__ == "__main__":
    main()
