"""
Roteamento `auto` entre estratégias de extração da etapa 5 (Fase 8, ADR-010).

Medido sobre o acervo real (35.552 documentos, 291.044 itens, mediana de 2 itens/doc):
`janela` vence quando o documento tem poucos itens (custo ≈ n_itens × texto); `completa`
amortiza melhor quando o documento tem muitos itens (custo ≈ texto + n_itens × tabela). A
escolha é SEMPRE por documento, nunca por run — é isso que produz o ganho de −38% de tokens
de entrada medido em docs/02_SCHEMA.md §6.1, não uma escolha global de estratégia.

`visao` fica de fora do roteamento por fórmula: é rota de EXCEÇÃO, acionada depois, só quando
`janela`/`completa` deixam o documento `suspeito`/`ilegivel` com itens suficientes para
justificar o custo por página (ver `etapas.e5_extrair`).
"""


def escolher_estrategia(n_itens: int, tamanho_texto_chars: int, janela_max: int,
                        tamanho_tabela: int = 2500) -> str:
    """`completa` quando `n_itens > tamanho_texto / (janela_max - tamanho_tabela)`;
    `janela` caso contrário. Com os defaults (janela_max=9000, tamanho_tabela=2500) o divisor
    é 6500 chars/item — docs/02_SCHEMA.md §6.1.
    """
    divisor = janela_max - tamanho_tabela
    if divisor <= 0 or n_itens <= 0:
        return "janela"
    return "completa" if n_itens > (tamanho_texto_chars / divisor) else "janela"
