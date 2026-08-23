"""
m02 — Prompts: `core/prompts.py` → `prompt` + `prompt_version` (v1 active).

Na Fase 2 o objetivo é modesto e específico: dar a `texto_classificacao.prompt_version_id` e a
`llm_call.prompt_version_id` uma linha real a que apontar. Migrar a EDIÇÃO de prompt para a
interface é entrega da Fase 6 — aqui os prompts continuam vindo do código.

Os prompts de `core/prompts.py` são FUNÇÕES que montam texto, não templates estáticos. Para
gravar um template de verdade, cada função é chamada com os próprios nomes dos parâmetros como
value (`"{description}"`), o que devolve o texto real com placeholders no lugar certo. É o mais
próximo do prompt em produção que se consegue sem reescrever `prompts.py` — e reescrever
`prompts.py` aqui seria mudar o método numa fase cujo objetivo é mudar a persistência.

Uso: python -m migracao.m02_prompts
"""

from pesquisa_precos.core import prompts
from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import execution as repo
from migracao._comum import Relatorio, cabecalho, console

_ITEM_API = {"numeroItem": "{numero_item}", "descricao_api": "{descricao_api}"}

# (name, descrição, capability, template). O name é a key estável — é o que a Fase 6 vai
# usar para casar a versão editada com o ponto de chamada no código.
DEFINICOES = (
    ("classificar_item",
     "Etapa 3 — classificação multi-label de categoria a partir da descrição do item PNCP",
     "chat",
     lambda: prompts.montar_prompt_classificar_item("{description}", "{unidade}")),
    ("termos_item",
     "Etapa 1 — gera termos de busca genéricos a partir de um item do catálogo",
     "chat",
     lambda: prompts.montar_prompt_termos_item("{name}", "{description}", "{tipo}",
                                               "{nome_grupo}")),
    ("extrair_item_pdf",
     "Etapa 5b — extração guiada de UM item a partir da janela de texto do PDF",
     "chat",
     lambda: prompts.montar_prompt_extrair_item_pdf("{janela_texto}", _ITEM_API)),
    ("extrair_itens",
     "Extração da lista de itens de um trecho de documento",
     "chat",
     lambda: prompts.montar_prompt_extrair_itens("{texto}")),
    ("comparar_par",
     "Etapa 6c — decide se item de catálogo e item PNCP são o mesmo item",
     "chat",
     lambda: prompts.montar_prompt_comparar_par("{texto_catalogo}", "{texto_item}")),
    ("comparar_item",
     "Comparação item PNCP × item de catálogo com o objeto da compra em context",
     "chat",
     lambda: prompts.montar_prompt_comparar_item(
         "{descricao_pncp}", "{objeto_compra}", "{nome_catalogo}", "{descricao_catalogo}")),
    ("busca_termos",
     "Geração de termos de busca a partir de name/descrição de catálogo",
     "chat",
     lambda: prompts.montar_prompt_busca("{name}", "{description}", "{categoria}")),
    ("extrair_tabela_pdf",
     "Etapa 5_alt_a — extrai a tabela de itens direto da imagem da página (visão)",
     "chat",
     prompts.montar_prompt_extrair_tabela_pdf),
    ("casar_item_tabela",
     "Etapa 5_alt_b — casa um item da API contra a tabela limpa extraída do PDF",
     "chat",
     lambda: prompts.montar_prompt_casar_item_tabela(_ITEM_API, [])),
)


def migrar() -> Relatorio:
    rel = Relatorio("m02 — prompts")
    with db.session() as s:
        for name, description, capability, montar in DEFINICOES:
            try:
                template = montar()
            except Exception as exc:  # noqa: BLE001
                # Um prompt que não renderiza com placeholders não pode derrubar a migração
                # inteira: os outros oito continuam válidos e o buraco fica visível no relatório.
                rel.aviso(f"{name}: não renderizou ({type(exc).__name__}: {exc})")
                rel.mais("failed")
                continue
            repo.upsert_prompt(s, name, description, capability, template, version=1, active=True)
            rel.mais("prompt_version v1 active")
    return rel


def main() -> None:
    cabecalho("m02 — prompts", [], "prompt, prompt_version")
    console.print(f"  banco  : {db.database_url()}")
    migrar().imprimir()


if __name__ == "__main__":
    main()
