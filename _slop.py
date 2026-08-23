"""Reescreve a docstring de topo de um módulo. Auxiliar temporário da limpeza."""
import io
import pathlib
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def troca_topo(caminho: str, nova: str) -> None:
    p = pathlib.Path(caminho)
    t = p.read_text(encoding="utf-8")
    if not t.startswith('"""'):
        raise SystemExit(f"{caminho}: nao comeca com docstring")
    fim = t.index('"""', 3) + 3
    p.write_text(nova.rstrip() + t[fim:], encoding="utf-8")
    print("ok", caminho)


def troca(caminho: str, antigo: str, novo: str, *, obrigatorio: bool = True) -> None:
    p = pathlib.Path(caminho)
    t = p.read_text(encoding="utf-8")
    if antigo not in t:
        if obrigatorio:
            raise SystemExit(f"{caminho}: trecho nao encontrado -> {antigo[:60]!r}")
        return
    p.write_text(t.replace(antigo, novo), encoding="utf-8")
    print("ok", caminho)
