"""
Orquestrador do pipeline — roda as etapas em sequência, para no primeiro erro e reporta.

Dois modos:
  --completo    pipeline inteiro (rodada inicial / recalibração). 0a só baixa o catálogo se
                faltando.
  --atualizar   rodada de atualização incremental. Diferenças em relação ao completo:
                  · 0a roda com --forcar (rebaixa o catálogo p/ detectar PDMs novos/removidos
                    e gerar o delta) — salvo --sem-catalogo;
                  · 2 roda com --atualizar (para de paginar no watermark + revisita pendentes).
                As demais etapas são resumíveis/agregadoras por natureza: só processam o novo
                (3, 5, 6a-embeddings, 6b, 6c) ou recomputam barato o conjunto todo (4, 7, 8).

Cada etapa é um módulo `pesquisa_precos.etapas.e*` invocado como subprocesso `python -m` (a
saída aparece ao vivo). A sequência e as flags aceitas por cada etapa vêm do **registry**
(`pesquisa_precos.etapas.registry`) e dos `Params` de cada uma — antes da Fase 1 as duas
coisas eram tabelas escritas à mão aqui, que precisavam ser lembradas a cada etapa nova.

Uso:
  python rodar.py --completo  [--provedor openrouter] [--remoto]
  python rodar.py --atualizar [--provedor openrouter] [--remoto] [--sem-catalogo]
  python rodar.py --atualizar --de 3          # começa na etapa 3 (retomar após corrigir algo)
  python rodar.py --completo  --dry-run        # só imprime a sequência
"""

import argparse
import subprocess
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from rich.console import Console
from rich.table import Table

from pesquisa_precos.config import paths
from pesquisa_precos.etapas import registry

console = Console()
RAIZ = paths.RAIZ
DATA = paths.DATA
PY = sys.executable

MODULO_BASE = "pesquisa_precos.etapas"


def montar_etapas(args) -> list[tuple[str, str]]:
    """Devolve a lista ordenada [(id, modulo)], a partir do registry."""
    return [(e.chave, e.modulo) for e in registry.ordem()]


def _aceita(chave: str, script: str, flag: str) -> bool:
    """A etapa expõe esta flag? Pergunta ao schema `Params` dela — sem tabela paralela."""
    try:
        return flag in registry.obter(chave).params_model.model_fields
    except KeyError:
        return False


def montar_comando(chave: str, script: str, args) -> list[str]:
    """Monta os argumentos de uma etapa conforme o modo e as flags que ela aceita."""
    extra: list[str] = []
    if _aceita(chave, script, "provedor"):
        extra += ["--provedor", args.provedor]
    if args.remoto and _aceita(chave, script, "remoto"):
        extra += ["--remoto"]
    if script == "e0a_catalogo" and args.atualizar and not args.sem_catalogo:
        extra += ["--forcar"]  # rebaixa o catálogo p/ detectar mudanças de PDM e gerar o delta
    if script == "e2_coletar" and args.atualizar:
        extra += ["--atualizar"]
    return [PY, "-m", f"{MODULO_BASE}.{script}", *extra]


def relatorio_final(resultados: list[tuple]) -> None:
    t = Table(title="Resumo da execução", show_lines=False)
    t.add_column("Etapa"); t.add_column("Script"); t.add_column("Status"); t.add_column("Tempo")
    for eid, script, status, seg in resultados:
        cor = "green" if status == "ok" else ("yellow" if status == "pulada" else "red")
        t.add_row(eid, script, f"[{cor}]{status}[/]", f"{seg:.0f}s" if seg else "—")
    console.print(t)
    # Contexto incremental: delta do catálogo e tamanho da exportação final.
    delta = paths.E0A_DELTA
    if delta.exists():
        import csv
        with open(delta, encoding="utf-8-sig") as f:
            linhas = list(csv.DictReader(f))
        novos = sum(1 for r in linhas if r.get("status") == "novo")
        rem = sum(1 for r in linhas if r.get("status") == "removido")
        console.print(f"[dim]Catálogo: {novos} códigos novos, {rem} removidos (delta 0a).[/]")
    export = paths.E8_CSV
    if export.exists():
        n = sum(1 for _ in open(export, encoding="utf-8-sig")) - 1
        console.print(f"[dim]Exportação final: {n} linhas → {export.name}[/]")


def main():
    ap = argparse.ArgumentParser(description="Orquestrador do pipeline itens-contratos-atas v3")
    modo = ap.add_mutually_exclusive_group(required=True)
    modo.add_argument("--completo", action="store_true", help="Pipeline inteiro do zero")
    modo.add_argument("--atualizar", action="store_true", help="Rodada de atualização incremental")
    ap.add_argument("--provedor", choices=["local", "openrouter"], default="local")
    ap.add_argument("--remoto", action="store_true", help="Usa a GPU remota nas etapas 6a/6b")
    ap.add_argument("--sem-catalogo", action="store_true",
                    help="No --atualizar, NÃO rebaixa o catálogo na 0a (assume que não mudou)")
    ap.add_argument("--de", metavar="ID", help="Começa nesta etapa (ex.: 3, 6a) — p/ retomar")
    ap.add_argument("--ate", metavar="ID", help="Para nesta etapa (inclusive)")
    ap.add_argument("--dry-run", action="store_true", help="Só imprime a sequência, não executa")
    args = ap.parse_args()

    etapas = montar_etapas(args)
    ids = [e[0] for e in etapas]
    ini = ids.index(args.de) if args.de else 0
    fim = ids.index(args.ate) + 1 if args.ate else len(etapas)
    if args.de and args.de not in ids:
        ap.error(f"--de {args.de} inválido. IDs: {', '.join(ids)}")
    if args.ate and args.ate not in ids:
        ap.error(f"--ate {args.ate} inválido. IDs: {', '.join(ids)}")
    selecionadas = etapas[ini:fim]

    modo_txt = "ATUALIZAR" if args.atualizar else "COMPLETO"
    console.print(f"[bold]Pipeline v3 — modo {modo_txt}[/] · provedor={args.provedor} · "
                  f"remoto={args.remoto}")
    for eid, script in selecionadas:
        cmd = montar_comando(eid, script, args)
        console.print(f"  [cyan]{eid:>2}[/] → {' '.join(cmd[1:])}")
    if args.dry_run:
        return

    resultados = []
    for eid, script in selecionadas:
        cmd = montar_comando(eid, script, args)
        console.rule(f"[bold]Etapa {eid} · {script}")
        t0 = time.time()
        proc = subprocess.run(cmd, cwd=str(RAIZ))
        seg = time.time() - t0
        if proc.returncode != 0:
            resultados.append((eid, script, f"ERRO (rc={proc.returncode})", seg))
            console.print(f"[bold red]Etapa {eid} falhou (rc={proc.returncode}). Parando.[/] "
                          f"Corrija e retome com: python rodar.py --{modo_txt.lower()} --de {eid}")
            relatorio_final(resultados)
            raise SystemExit(proc.returncode)
        resultados.append((eid, script, "ok", seg))
    console.print("[bold green]Pipeline concluído.[/]")
    relatorio_final(resultados)


if __name__ == "__main__":
    main()
