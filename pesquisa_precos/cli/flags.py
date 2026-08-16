"""
Geração das flags do CLI a partir dos `Params` (Pydantic) de cada etapa.

Fase 1, entrega 4: "flags **geradas a partir dos `Params`**, não escritas à mão". O motivo é
o de sempre neste projeto — duplicação silenciosa. Enquanto as flags eram `argparse` escrito
à mão em cada etapa, o default do código e o default do CLI podiam divergir sem que nada
falhasse; o sintoma seria um run com parâmetro diferente do que o operador achou que pediu.
Com uma fonte só, o mesmo schema serve a CLI, à validação, à API e (Fase 6) ao formulário web.

Regras de mapeamento:
  - `retry_erros` → `--retry-erros` (mesmo nome que o `argparse` usava; nada muda para quem já
    tem o comando no histórico do terminal);
  - `bool` vira flag simples (`--forcar`), NÃO o par `--forcar/--no-forcar` do Typer — é o que
    o `argparse` fazia com `action="store_true"`;
  - `Literal["local","openrouter"]` vira `str`: quem valida é o Pydantic, não o Typer, para
    que CLI, API e web reprovem exatamente o mesmo valor.
"""

import inspect
from typing import Any, Literal, get_args, get_origin

import typer
from pydantic import BaseModel


def _tipo_para_cli(anotacao: Any) -> Any:
    """Anotação que o Typer entende. Literal vira str/int conforme os seus valores."""
    origem = get_origin(anotacao)
    if origem is Literal:
        valores = get_args(anotacao)
        return type(valores[0]) if valores else str
    if origem is not None:  # Optional[...] / X | None
        args = [a for a in get_args(anotacao) if a is not type(None)]
        if len(args) == 1:
            interno = _tipo_para_cli(args[0])
            return interno | None
    return anotacao


def _ajuda(campo, anotacao: Any) -> str:
    ajuda = campo.description or ""
    if get_origin(anotacao) is Literal:
        ajuda = (ajuda + f" [{'|'.join(str(v) for v in get_args(anotacao))}]").strip()
    return ajuda


def parametros_typer(modelo: type[BaseModel]) -> list[inspect.Parameter]:
    """Um `inspect.Parameter` por campo do modelo, já com o `typer.Option` correspondente."""
    parametros = []
    for nome, campo in modelo.model_fields.items():
        flag = "--" + nome.replace("_", "-")
        anotacao = campo.annotation
        tipo = _tipo_para_cli(anotacao)
        default = campo.get_default(call_default_factory=True)
        opcao = typer.Option(default, flag, help=_ajuda(campo, anotacao))
        parametros.append(inspect.Parameter(
            nome, inspect.Parameter.KEYWORD_ONLY, default=opcao, annotation=tipo))
    return parametros


def comando_para(modelo: type[BaseModel], corpo) -> Any:
    """Embrulha `corpo(params)` numa função com a assinatura que o Typer sabe ler.

    O Typer descobre as opções por `inspect.signature`; como os campos só existem em tempo de
    execução, a assinatura é montada aqui e colada na função.
    """
    parametros = parametros_typer(modelo)

    def comando(**kwargs):
        return corpo(modelo(**kwargs))

    comando.__signature__ = inspect.Signature(parametros)
    comando.__annotations__ = {p.name: p.annotation for p in parametros}
    return comando
