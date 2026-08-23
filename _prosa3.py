"""Última onda: palavra inglesa solta dentro de prosa portuguesa.

As ondas anteriores usavam heurística de aspas triplas para achar docstring, e isso
confundia string SQL com prosa — o que já quebrou um `CAST(x AS tipo)` e uma lista de
colunas. Aqui a identificação é exata: `ast` diz quais strings são docstring de módulo,
classe ou função, e `tokenize` diz quais linhas são comentário. Nada mais é tocado.

O que fica em inglês, mesmo em prosa: o que está entre crases, porque ali o nome técnico é
justamente o assunto (`run_step.status`, `provider_capability`).
"""
import ast
import io
import pathlib
import re
import sys
import tokenize

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

VOLTA = {
    "step": "etapa", "steps": "etapas", "key": "chave", "keys": "chaves",
    "model": "modelo", "models": "modelos", "provider": "provedor",
    "providers": "provedores", "active": "ativo", "label": "rótulo",
    "labels": "rótulos", "message": "mensagem", "messages": "mensagens",
    "source": "origem", "description": "descrição", "capability": "capacidade",
    "capabilities": "capacidades", "value": "valor", "values": "valores",
    "resolved": "resolvido", "name": "nome", "names": "nomes", "notes": "notas",
    "reason": "motivo", "mode": "modo", "attempts": "tentativas",
    "processed": "processados", "success": "sucesso", "priority": "prioridade",
    "version": "versão", "context": "contexto", "level": "nível",
}

ALVO = "|".join(sorted(VOLTA, key=len, reverse=True))
RX = re.compile(rf"(?<![A-Za-z0-9_`.\"'-])({ALVO})(?![A-Za-z0-9_`\"'(=\[.-])")
CRASE = re.compile(r"`[^`]*`")


def _troca_fora_de_crases(txt: str) -> str:
    partes, fim = [], 0
    for m in CRASE.finditer(txt):
        partes.append(RX.sub(lambda x: VOLTA[x.group(1)], txt[fim:m.start()]))
        partes.append(m.group(0))
        fim = m.end()
    partes.append(RX.sub(lambda x: VOLTA[x.group(1)], txt[fim:]))
    return "".join(partes)


def _linhas_de_docstring(origem: str) -> set[int]:
    """Números de linha (1-based) ocupados por docstrings de módulo, classe e função."""
    alvo: set[int] = set()
    try:
        arvore = ast.parse(origem)
    except SyntaxError:
        return alvo
    for no in ast.walk(arvore):
        if not isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef,
                               ast.AsyncFunctionDef)):
            continue
        corpo = getattr(no, "body", None)
        if not corpo:
            continue
        primeiro = corpo[0]
        if (isinstance(primeiro, ast.Expr) and isinstance(primeiro.value, ast.Constant)
                and isinstance(primeiro.value.value, str)):
            alvo.update(range(primeiro.lineno, primeiro.end_lineno + 1))
    return alvo


def _comentarios(caminho: pathlib.Path) -> dict[int, int]:
    """linha (1-based) -> coluna onde o comentário começa.

    A coluna importa: numa linha com comentário ao lado de código, só o que vem depois do
    `#` é prosa. Trocar a linha inteira renomearia o código junto.
    """
    alvo: dict[int, int] = {}
    with caminho.open("rb") as fh:
        try:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == tokenize.COMMENT:
                    alvo[tok.start[0]] = tok.start[1]
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass
    return alvo


def processa(caminho: pathlib.Path) -> bool:
    origem = caminho.read_text(encoding="utf-8")
    docs = _linhas_de_docstring(origem)
    coments = _comentarios(caminho)
    if not docs and not coments:
        return False
    linhas = origem.split("\n")
    mudou = False
    for i, linha in enumerate(linhas):
        n = i + 1
        if n in docs:
            nova = _troca_fora_de_crases(linha)
        elif n in coments:
            col = coments[n]
            nova = linha[:col] + _troca_fora_de_crases(linha[col:])
        else:
            continue
        if nova != linha:
            linhas[i] = nova
            mudou = True
    if mudou:
        caminho.write_text("\n".join(linhas), encoding="utf-8")
    return mudou


def main() -> None:
    n = 0
    for pat in ("pesquisa_precos/**/*.py", "migracao/**/*.py", "tools/**/*.py"):
        for p in pathlib.Path(".").glob(pat):
            if p.is_file() and not p.name.startswith("_prosa") and processa(p):
                n += 1
    print("arquivos:", n)


if __name__ == "__main__":
    main()
