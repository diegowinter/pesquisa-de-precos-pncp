"""Devolve ao português as palavras que o rename anglicizou dentro de texto corrido.

A regra do projeto é: identificador em inglês, prosa em português. Os sweeps de rename
trocaram o token em todo lugar, inclusive dentro de comentários, docstrings e strings de
UI. Aqui a troca é ancorada em artigo/preposição em português imediatamente antes — o que
só acontece em prosa, nunca em código.
"""
import io
import pathlib
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# palavra em inglês -> forma portuguesa (singular, minúscula)
VOLTA = {
    "step": "etapa", "key": "chave", "model": "modelo", "provider": "provedor",
    "active": "ativo", "label": "rótulo", "message": "mensagem", "source": "origem",
    "description": "descrição", "capability": "capacidade", "batch": "lote",
    "capabilities": "capacidades", "steps": "etapas", "keys": "chaves",
    "providers": "provedores", "models": "modelos", "labels": "rótulos",
}

# determinantes que marcam prosa em português; o gênero decide a forma final
FEM = {"a", "da", "na", "uma", "as", "das", "nas", "à", "essa", "esta", "aquela"}
MASC = {"o", "do", "no", "ao", "um", "os", "dos", "nos", "esse", "este", "aquele"}

# forma feminina quando existir
GENERO = {
    "etapa": ("etapa", "f"), "chave": ("chave", "f"), "modelo": ("modelo", "m"),
    "provedor": ("provedor", "m"), "ativo": ("ativo", "m"), "rótulo": ("rótulo", "m"),
    "mensagem": ("mensagem", "f"), "origem": ("origem", "f"), "descrição": ("descrição", "f"),
    "capacidade": ("capacidade", "f"), "lote": ("lote", "m"),
    "capacidades": ("capacidades", "f"), "etapas": ("etapas", "f"), "chaves": ("chaves", "f"),
    "provedores": ("provedores", "m"), "modelos": ("modelos", "m"), "rótulos": ("rótulos", "m"),
}

DET = "|".join(sorted(FEM | MASC, key=len, reverse=True))
ALVO = "|".join(sorted(VOLTA, key=len, reverse=True))
RX = re.compile(rf"\b({DET})\s+({ALVO})\b", re.I)


def _ajusta(det: str, palavra_en: str) -> str:
    pt, genero = GENERO[VOLTA[palavra_en.lower()]]
    d = det.lower()
    # concorda o determinante com o gênero da palavra portuguesa
    if genero == "f" and d in MASC:
        troca = {"o": "a", "do": "da", "no": "na", "ao": "à", "um": "uma",
                 "os": "as", "dos": "das", "nos": "nas", "esse": "essa",
                 "este": "esta", "aquele": "aquela"}
        d = troca.get(d, d)
    elif genero == "m" and d in FEM:
        troca = {"a": "o", "da": "do", "na": "no", "uma": "um", "as": "os",
                 "das": "dos", "nas": "nos", "à": "ao", "essa": "esse",
                 "esta": "este", "aquela": "aquele"}
        d = troca.get(d, d)
    if det[0].isupper():
        d = d.capitalize()
    return f"{d} {pt}"


def main() -> None:
    n = 0
    for pat in ("pesquisa_precos/**/*.py", "migracao/**/*.py", "tools/**/*.py",
                "pesquisa_precos/web/templates/*.html"):
        for p in pathlib.Path(".").glob(pat):
            if not p.is_file():
                continue
            t = o = p.read_text(encoding="utf-8")
            t = RX.sub(lambda mo: _ajusta(mo.group(1), mo.group(2)), t)
            if t != o:
                p.write_text(t, encoding="utf-8")
                n += 1
    print("arquivos com prosa corrigida:", n)


if __name__ == "__main__":
    main()
