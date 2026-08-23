"""Segunda onda da correção de prosa: a palavra em inglês seguida de preposição/verbo.

Casos como "key de API", "provider de embedding", "uma step com gate" — que a primeira onda
não pega porque não vêm precedidos de determinante. Só em comentário ou docstring, e nunca
dentro de crases, onde o nome técnico é o assunto.
"""
import io
import pathlib
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

VOLTA = {
    "step": "etapa", "steps": "etapas", "key": "chave", "keys": "chaves",
    "model": "modelo", "models": "modelos", "provider": "provedor",
    "providers": "provedores", "active": "ativo", "label": "rótulo",
    "labels": "rótulos", "message": "mensagem", "source": "origem",
    "description": "descrição", "capability": "capacidade",
    "capabilities": "capacidades",
}
SEGUINTES = ("que", "de", "do", "da", "dos", "das", "em", "no", "na", "com", "por",
             "para", "foi", "sem", "entre", "vai", "pode", "e", "ou", "já", "ainda",
             "não", "mora", "fica", "aponta", "atende")

ALVO = "|".join(sorted(VOLTA, key=len, reverse=True))
SEG = "|".join(SEGUINTES)
RX = re.compile(rf"(?<![A-Za-z0-9_`.]) ?({ALVO}) ({SEG})\b")
CRASE = re.compile(r"`[^`]*`")


def _troca(mo: re.Match) -> str:
    prefixo = " " if mo.group(0).startswith(" ") else ""
    return f"{prefixo}{VOLTA[mo.group(1)]} {mo.group(2)}"


def processa(texto: str) -> str:
    saida = []
    dentro = False
    for linha in texto.split("\n"):
        aspas = linha.count('"""') + linha.count("'''")
        prosa = dentro or aspas > 0 or linha.lstrip().startswith("#")
        if prosa and "SELECT" not in linha.upper():
            partes, fim = [], 0
            for m in CRASE.finditer(linha):
                partes.append(RX.sub(_troca, linha[fim:m.start()]))
                partes.append(m.group(0))
                fim = m.end()
            partes.append(RX.sub(_troca, linha[fim:]))
            linha = "".join(partes)
        if aspas % 2 == 1:
            dentro = not dentro
        saida.append(linha)
    return "\n".join(saida)


def main() -> None:
    n = 0
    for pat in ("pesquisa_precos/**/*.py", "migracao/**/*.py", "tools/**/*.py"):
        for p in pathlib.Path(".").glob(pat):
            if not p.is_file() or p.name.startswith("_prosa"):
                continue
            t = o = p.read_text(encoding="utf-8")
            t = processa(t)
            if t != o:
                p.write_text(t, encoding="utf-8")
                n += 1
    print("arquivos:", n)


if __name__ == "__main__":
    main()
