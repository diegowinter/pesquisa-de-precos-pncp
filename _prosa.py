"""Devolve ao português as palavras que o rename anglicizou dentro de texto corrido.

A regra do projeto é: identificador em inglês, prosa em português. Os sweeps de rename
trocaram o token em todo lugar, inclusive dentro de comentários e docstrings.

Duas travas contra estragar código: a linha precisa ser comentário ou estar dentro de uma
docstring de módulo/função, e a palavra só é trocada quando vem depois de um determinante
português em minúscula — o que exclui o `AS` de `CAST(x AS tipo)`. Trechos entre crases
(`provider`, `run_step`) são preservados: ali o nome técnico é o assunto.
"""
import io
import pathlib
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

VOLTA = {
    "step": ("etapa", "f"), "steps": ("etapas", "f"),
    "key": ("chave", "f"), "keys": ("chaves", "f"),
    "model": ("modelo", "m"), "models": ("modelos", "m"),
    "provider": ("provedor", "m"), "providers": ("provedores", "m"),
    "active": ("ativo", "m"), "label": ("rótulo", "m"), "labels": ("rótulos", "m"),
    "message": ("mensagem", "f"), "source": ("origem", "f"),
    "description": ("descrição", "f"), "capability": ("capacidade", "f"),
    "capabilities": ("capacidades", "f"), "batch": ("lote", "m"),
}

FEM = {"a", "da", "na", "uma", "as", "das", "nas", "essa", "esta", "aquela"}
MASC = {"o", "do", "no", "ao", "um", "os", "dos", "nos", "esse", "este", "aquele"}
P_FEM = {"o": "a", "do": "da", "no": "na", "ao": "à", "um": "uma", "os": "as",
         "dos": "das", "nos": "nas", "esse": "essa", "este": "esta", "aquele": "aquela"}
P_MASC = {"a": "o", "da": "do", "na": "no", "uma": "um", "as": "os", "das": "dos",
          "nas": "nos", "essa": "esse", "esta": "este", "aquela": "aquele"}

DET = "|".join(sorted(FEM | MASC, key=len, reverse=True))
ALVO = "|".join(sorted(VOLTA, key=len, reverse=True))
# determinante em MINÚSCULA apenas: descarta o `AS` do SQL e o início de frase ambíguo
RX = re.compile(rf"(?<![A-Za-z0-9_`])({DET}) ({ALVO})\b")
CRASE = re.compile(r"`[^`]*`")


def _ajusta(mo: re.Match) -> str:
    det, en = mo.group(1), mo.group(2)
    pt, genero = VOLTA[en]
    if genero == "f" and det in MASC:
        det = P_FEM.get(det, det)
    elif genero == "m" and det in FEM:
        det = P_MASC.get(det, det)
    return f"{det} {pt}"


def _linha_de_prosa(linha: str, dentro_docstring: bool) -> bool:
    return dentro_docstring or linha.lstrip().startswith("#")


def processa(texto: str) -> str:
    saida = []
    dentro = False
    for linha in texto.split("\n"):
        # conta as aspas triplas para saber se a próxima linha está dentro da docstring
        abre_fecha = linha.count('"""') + linha.count("'''")
        prosa = _linha_de_prosa(linha, dentro or abre_fecha > 0)
        if prosa:
            # preserva o que está entre crases
            partes = []
            fim = 0
            for m in CRASE.finditer(linha):
                partes.append(RX.sub(_ajusta, linha[fim:m.start()]))
                partes.append(m.group(0))
                fim = m.end()
            partes.append(RX.sub(_ajusta, linha[fim:]))
            linha = "".join(partes)
        if abre_fecha % 2 == 1:
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
    print("arquivos com prosa corrigida:", n)


if __name__ == "__main__":
    main()
