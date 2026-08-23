"""
Infraestrutura compartilhada pelos scripts de migração CSV → PostgreSQL.

As quatro regras invioláveis de docs/05_MIGRACAO.md §1 estão implementadas AQUI, não em cada
script, justamente para não dependerem de alguém lembrar:

  idempotente   → toda escrita passa por `db/copy.py`, que faz `ON CONFLICT`;
  resumível     → `Retomada` grava quantas linhas do CSV já foram consumidas;
  origem só p/ leitura → nenhuma função deste pacote abre CSV em modo de escrita;
  streaming     → `ler_csv()` é um gerador; `2_itens_coletados.csv` tem 746 MB e
                  `5_pdf_texto.csv` tem 2,6 GB, e nenhum dos dois cabe confortavelmente
                  em memória via `pd.read_csv`.

`csv.field_size_limit` é elevado logo no import: há campos com uma página inteira de PDF, e o
limite padrão do módulo `csv` (131 kB) aborta a leitura com `_csv.Error` no meio do arquivo —
falha que aparece como "migração parou na linha 400 mil", não como "campo grande demais".
"""

import csv
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from rich.console import Console

from pesquisa_precos.config import paths

csv.field_size_limit(10 ** 9)

console = Console()

# Estado de retomada de cada script, fora de `data/` para não se misturar aos checkpoints
# das etapas (que são outra coisa: estado de resumo da pipeline, não da migração).
CHECKPOINTS_MIGRACAO = paths.CHECKPOINTS / "migracao"


# ── Conversões de tipo ──────────────────────────────────────────────────────────────
#
# Os CSVs guardam tudo como texto, inclusive vazio para nulo. Converter na fronteira é o que
# permite `numeric(18,4)` e `date` no banco em vez de `text` — e, no caso do preço, é o que
# cumpre a regra "preço nunca é float" (docs/08_CONVENCOES.md §5.8).

def txt(valor) -> str | None:
    """String não-vazia, ou None. Espaço em branco conta como vazio."""
    s = (valor or "").strip()
    return s or None


def dec(valor) -> Decimal | None:
    """Decimal, ou None. Aceita o ponto decimal do CSV (que veio de `float` do pandas).

    Nunca passa por `float`: `Decimal(str(...))` preserva o que está escrito. Converter para
    float e voltar introduziria erro na quarta casa em valores de contrato de milhões.
    """
    s = (valor or "").strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def inteiro(valor) -> int | None:
    s = (valor or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def data(valor) -> date | None:
    """Data ISO, tolerando o sufixo de hora ('2026-07-10T12:22:06.340806706')."""
    s = (valor or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s.split("T")[0].split(" ")[0])
    except ValueError:
        return None


def timestamp(valor) -> datetime | None:
    """Timestamp ISO. Os nanossegundos que a API do PNCP devolve não cabem em `datetime`
    (que vai só a microssegundo), então a fração é truncada em 6 dígitos."""
    s = (valor or "").strip()
    if not s:
        return None
    if "." in s:
        inteira, _, fracao = s.partition(".")
        s = f"{inteira}.{fracao[:6]}"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return data(s) and datetime.combine(data(s), datetime.min.time())


def lista_pipe(valor) -> list[str]:
    """Campo multivalorado separado por '|' (categorias, conceitos_origem, codigos_catalogo)."""
    return [p for p in (valor or "").split("|") if p.strip()]


# ── Leitura em streaming ────────────────────────────────────────────────────────────

def ler_csv(caminho: Path, encoding: str = "utf-8") -> Iterator[dict]:
    """Gera dicts do CSV, uma linha por vez. Nunca materializa o arquivo.

    `utf-8-sig` deve ser passado explicitamente para os arquivos com BOM
    (`0a_catalogo_filtrado.csv`, `8_export_snapshot.csv`) — sem isso a PRIMEIRA coluna do
    cabeçalho vem com '\\ufeff' colado e o `row["tipo"]` estoura com KeyError.
    """
    with open(caminho, encoding=encoding, newline="") as f:
        yield from csv.DictReader(f)


def estimar_linhas(caminho: Path) -> int:
    """LIMITE SUPERIOR do número de registros, contando '\\n' em blocos de 1 MB.

    Não é a contagem exata, e o nome diz isso de propósito: as descrições do PNCP contêm
    quebras de linha DENTRO de campos entre aspas (um contrato de 78 mil `\\n` tem 60 mil
    registros). Contar de verdade exigiria uma passada completa do parser de CSV — cara
    justamente nos arquivos onde a barra de progresso importa, que são os de gigabytes.

    Serve para a barra de progresso, que chega ao fim antes dos 100%. **Não usar em
    comparação de contagem**: um `if migrados < estimar_linhas(...)` dispararia alarme falso
    em todo arquivo com descrição multilinha, ou seja, em todos.
    """
    with open(caminho, "rb") as f:
        n = sum(bloco.count(b"\n") for bloco in iter(lambda: f.read(1 << 20), b""))
    return max(n - 1, 0)


def existe(caminho: Path) -> bool:
    return caminho.exists() and caminho.stat().st_size > 0


# ── Retomada ────────────────────────────────────────────────────────────────────────

@dataclass
class Retomada:
    """Quantas linhas do CSV este script já consumiu.

    O contador é por LINHA DE ORIGEM, não por linha inserida: os CSVs de origem são
    somente-leitura e append-only, então "pular as N primeiras" é estável entre execuções.
    Contar destino não serviria — o destino dedupa, e o número não voltaria a bater.

    O avanço só é gravado DEPOIS que o lote foi comitado no banco. Gravar antes faria uma
    interrupção no meio do lote pular linhas nunca inseridas — o mesmo modo de falha que
    docs/08_CONVENCOES.md §5.3 descreve para o resultado de LLM.
    """

    nome: str
    linhas: int = 0

    @property
    def arquivo(self) -> Path:
        return CHECKPOINTS_MIGRACAO / f"{self.nome}.json"

    @classmethod
    def carregar(cls, nome: str) -> "Retomada":
        r = cls(nome)
        if r.arquivo.exists():
            try:
                r.linhas = int(json.loads(r.arquivo.read_text("utf-8"))["linhas"])
            except (ValueError, KeyError, OSError):
                r.linhas = 0
        return r

    def avancar(self, quantas: int) -> None:
        self.linhas += quantas
        self.salvar()

    def salvar(self) -> None:
        CHECKPOINTS_MIGRACAO.mkdir(parents=True, exist_ok=True)
        self.arquivo.write_text(
            json.dumps({"linhas": self.linhas, "em": datetime.now().isoformat()}),
            encoding="utf-8")

    def zerar(self) -> None:
        self.linhas = 0
        self.arquivo.unlink(missing_ok=True)


# ── Relatório ───────────────────────────────────────────────────────────────────────

class Relatorio:
    """Contadores nomeados + impressão no fim.

    "Migrou, deve estar ok" é explicitamente proibido (docs/05_MIGRACAO.md §4). Todo script
    termina imprimindo o que fez E o que deixou de fazer — linhas ignoradas, chaves que não
    resolveram, divergências. O número que não aparece é o que ninguém investiga.
    """

    def __init__(self, titulo: str) -> None:
        self.titulo = titulo
        self.contadores: dict[str, int] = {}
        self.avisos: list[str] = []

    def mais(self, chave: str, n: int = 1) -> None:
        self.contadores[chave] = self.contadores.get(chave, 0) + n

    def aviso(self, msg: str) -> None:
        self.avisos.append(msg)

    def imprimir(self) -> None:
        console.print(f"\n[bold]{self.titulo}[/]")
        largura = max((len(k) for k in self.contadores), default=0)
        for chave, valor in self.contadores.items():
            console.print(f"  {chave:<{largura}}  {valor:>12,}".replace(",", "."))
        for msg in self.avisos:
            console.print(f"  [yellow]⚠ {msg}[/]")


def cabecalho(titulo: str, origem: Path | list[Path], destino: str) -> None:
    origens = origem if isinstance(origem, list) else [origem]
    console.print(f"\n[bold cyan]{titulo}[/]")
    for o in origens:
        marca = "[green]✓[/]" if existe(o) else "[red]✗ ausente[/]"
        console.print(f"  origem : {o.name} {marca}")
    console.print(f"  destino: {destino}")
