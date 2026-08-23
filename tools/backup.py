"""
Backup do PostgreSQL (Fase 9, docs/04_FASES.md item 4) — `pg_dump` via subprocess, lendo
`DATABASE_URL` do `.env` (mesma fonte da verdade de `db.session.database_url`; nunca uma segunda
URL hardcoded — ver docs/08_CONVENCOES.md §"nunca escreva um caminho literal", mesmo princípio
aplicado a credencial de banco).

Nome do arquivo é datado (`pesquisa_precos_AAAAMMDD_HHMMSS.dump`), formato `-Fc` (custom,
comprimido, restaurável com `pg_restore` — inclusive parcialmente, tabela por tabela, o que um
`.sql` puro não permite).

Verificação de integridade (sem exigir Postgres extra): arquivo não-vazio + assinatura binária
do formato custom do pg_dump (`PGDMP` nos primeiros bytes). Restauração de fato — `pg_restore
--list` ou um banco descartável — é o passo forte, mas exige um Postgres local disponível;
documentado abaixo em vez de automatizado, para não amarrar o backup a infraestrutura extra.

Uso:
    python tools/backup.py                    # dump em tools/backups/
    python tools/backup.py --destino D:/backups
    python tools/backup.py --verificar caminho/para/arquivo.dump

Procedimento de restauração/validação (manual, documentado em vez de automatizado):
    1. `createdb pesquisa_precos_teste_restore` num Postgres local descartável.
    2. `pg_restore --dbname=pesquisa_precos_teste_restore --no-owner arquivo.dump`
    3. `python -m migracao.validar` (ou uma contagem manual de tabelas-chave) contra o banco
       restaurado, comparando com o original.
    4. `dropdb pesquisa_precos_teste_restore`.
   Não é automatizado aqui porque exige um segundo Postgres disponível — o que não pode ser
   assumido no ambiente onde o backup roda (CLAUDE.md: Claude só lê/inspeciona, não sobe infra).
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from pesquisa_precos.db.session import database_url  # noqa: E402

DESTINO_PADRAO = Path(__file__).resolve().parent / "backups"

# Assinatura binária do formato custom do pg_dump (`-Fc`) — os 5 primeiros bytes do arquivo.
ASSINATURA_PGDUMP = b"PGDMP"


def url_para_args_pg_dump(url_sqlalchemy: str) -> list[str]:
    """`postgresql+psycopg://user:pass@host:port/db` → args de conexão do `pg_dump`.

    `pg_dump` não entende o driver `+psycopg` do SQLAlchemy — só o esquema `postgresql://`
    puro, daí o parse manual em vez de repassar a URL inteira.
    """
    limpa = url_sqlalchemy.replace("postgresql+psycopg://", "postgresql://", 1)
    p = urlparse(limpa)
    args = []
    if p.hostname:
        args += ["--host", p.hostname]
    if p.port:
        args += ["--port", str(p.port)]
    if p.username:
        args += ["--username", p.username]
    banco = p.path.lstrip("/") or "pesquisa_precos"
    args += [banco]
    return args


def nome_arquivo(agora: datetime | None = None) -> str:
    agora = agora or datetime.now()
    return f"pesquisa_precos_{agora:%Y%m%d_%H%M%S}.dump"


def rodar_pg_dump(url_sqlalchemy: str, destino: Path, *, executavel: str = "pg_dump") -> Path:
    """Roda `pg_dump -Fc` e devolve o caminho do arquivo gerado. A senha vai por `PGPASSWORD`
    no ambiente do subprocesso (não na linha de comando, que ficaria visível em `ps`/histórico)."""
    destino.mkdir(parents=True, exist_ok=True)
    arquivo = destino / nome_arquivo()
    p = urlparse(url_sqlalchemy.replace("postgresql+psycopg://", "postgresql://", 1))
    import os
    ambiente = dict(os.environ)
    if p.password:
        ambiente["PGPASSWORD"] = p.password

    comando = [executavel, "-Fc", "--file", str(arquivo), *url_para_args_pg_dump(url_sqlalchemy)]
    resultado = subprocess.run(comando, env=ambiente, capture_output=True, text=True)
    if resultado.returncode != 0:
        raise SystemExit(f"pg_dump falhou (código {resultado.returncode}): {resultado.stderr}")
    return arquivo


def verificar_integridade(arquivo: Path) -> tuple[bool, str]:
    """Checagem leve: arquivo existe, não está vazio, e começa com a assinatura do formato
    custom do pg_dump. Não abre o dump inteiro (pode ter GBs) — só o cabeçalho."""
    if not arquivo.exists():
        return False, f"{arquivo} não existe"
    tamanho = arquivo.stat().st_size
    if tamanho == 0:
        return False, f"{arquivo} está vazio"
    with open(arquivo, "rb") as f:
        cabecalho = f.read(len(ASSINATURA_PGDUMP))
    if cabecalho != ASSINATURA_PGDUMP:
        return False, (f"{arquivo} não tem a assinatura do formato pg_dump custom "
                       f"(esperado {ASSINATURA_PGDUMP!r}, achado {cabecalho!r})")
    return True, f"{arquivo} ({tamanho:,} bytes) — assinatura OK"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--destino", type=Path, default=DESTINO_PADRAO)
    ap.add_argument("--pg-dump", default="pg_dump", help="Caminho do executável pg_dump")
    ap.add_argument("--verificar", type=Path, default=None,
                    help="Só verifica a integridade de um dump já existente (não gera novo)")
    args = ap.parse_args()

    if args.verificar:
        ok, msg = verificar_integridade(args.verificar)
        print(("OK: " if ok else "FALHOU: ") + msg)
        return 0 if ok else 1

    print(f"Backup de {database_url()!r} → {args.destino}")
    arquivo = rodar_pg_dump(database_url(), args.destino, executavel=args.pg_dump)
    ok, msg = verificar_integridade(arquivo)
    print(("OK: " if ok else "FALHOU: ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
