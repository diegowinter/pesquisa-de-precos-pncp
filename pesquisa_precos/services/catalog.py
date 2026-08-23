"""
Serviço da curadoria de catálogo — o que a tela `/catalog` e a etapa 0b compartilham.

Nenhuma rota fala com o banco direto (docs/06_API_E_WEB.md): a tela chama daqui, a etapa
chama o mesmo repo. Editar a allow-list aqui NÃO redereiva `catalogo_item` — aplicar o corte
é o trabalho da etapa 0b, com gate. É essa separação que faz a tela ser segura de mexer: até
aprovar a 0b, nada muda no escopo do pipeline.
"""

from typing import Any

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import curation as repo


def listar_permitidos(tipo: str | None = None, incluir_inativos: bool = True) -> list[dict]:
    with db.session() as sessao:
        return repo.listar_permitidos(sessao, tipo, incluir_inativos=incluir_inativos)


def candidatos(tipo: str, *, busca: str | None = None,
               limite: int = 100) -> tuple[list[dict], int]:
    """Devolve `(recorte, total)` — a lista é limitada, e o total é o que diz ao operador se
    vale refinar a busca."""
    with db.session() as sessao:
        return (repo.pdms_candidatos(sessao, tipo, busca=busca, limite=limite),
                repo.contar_candidatos(sessao, tipo, busca=busca))


def permitir(tipo: str, codigo: str, *, name: str | None = None,
             observacao: str | None = None, created_by: str | None = None) -> None:
    with db.session() as sessao:
        repo.permitir(sessao, tipo, codigo, name=name, observacao=observacao,
                      created_by=created_by)


def revogar(tipo: str, codigo: str, *, reason: str | None = None) -> int:
    with db.session() as sessao:
        return repo.revogar(sessao, tipo, codigo, reason=reason)


def resumo() -> dict[str, Any]:
    """Os números do topo da tela: tamanho do catálogo baixado, da allow-list, e quantos
    itens o corte deixaria passar HOJE — sem aplicar nada."""
    from sqlalchemy import text

    with db.session() as sessao:
        return {
            "linhas_no_catalogo": repo.contar_raw(sessao),
            "codigos_permitidos": sessao.execute(
                text("SELECT count(*) FROM pdm_permitido WHERE active")).scalar_one(),
            "itens_no_corte": sessao.execute(text("""
                SELECT count(*) FROM catalogo_raw r
                  JOIN pdm_permitido p
                    ON p.tipo = r.tipo AND p.active
                   AND p.codigo = CASE WHEN r.tipo = 'material'
                                       THEN r.codigo_pdm ELSE r.codigo END
            """)).scalar_one(),
            "itens_derivados": sessao.execute(
                text("SELECT count(*) FROM catalogo_item WHERE active")).scalar_one(),
        }
