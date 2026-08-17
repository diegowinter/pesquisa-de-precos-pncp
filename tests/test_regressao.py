"""
Suite de regressão de qualidade (Fase 9, item 1 de docs/04_FASES.md).

Critério de aceite: "roda em <5min e reprova uma degradação introduzida de propósito". Este
arquivo é justamente esse teste — usa a fixture sintética (`tests/fixtures/
rotulos_sinteticos.csv`, 30 rótulos + 2 pendentes descartados) para rodar em milissegundos, sem
tocar o banco.
"""

from pathlib import Path

from pesquisa_precos.core.regressao import avaliar
from ferramentas.regressao import carregar_da_fixture

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rotulos_sinteticos.csv"

# Thresholds "de produção" — os defaults de .env.example (RERANK_T_ACEITA/RERANK_T_REJEITA).
T_ACEITA_OK = 0.80
T_REJEITA_OK = 0.30

LIMIAR_APROVACAO = 0.85


class TestRegressaoComThresholdsAtuais:
    def test_precisao_e_recall_altos_com_thresholds_calibrados(self):
        rotulos = carregar_da_fixture(FIXTURE)
        resultado = avaliar(rotulos, t_aceita=T_ACEITA_OK, t_rejeita=T_REJEITA_OK)
        assert resultado.precisao is not None
        assert resultado.recall is not None
        assert resultado.precisao >= LIMIAR_APROVACAO
        assert resultado.recall >= LIMIAR_APROVACAO

    def test_pendente_e_descartado_da_amostra(self):
        """`decisao_final='pendente'` não é gabarito — não pode contar nem a favor nem contra."""
        rotulos = carregar_da_fixture(FIXTURE)
        resultado = avaliar(rotulos, t_aceita=T_ACEITA_OK, t_rejeita=T_REJEITA_OK)
        n_pendentes = sum(1 for r in rotulos if r.decisao_final == "pendente")
        assert n_pendentes > 0  # a fixture tem 2 de propósito
        assert resultado.n_amostra == len(rotulos) - n_pendentes


class TestSuiteReprovaDegradacao:
    """O critério de aceite da Fase 9: a suite precisa REPROVAR quando alguém degrada um
    threshold de propósito — senão ela não vale nada como rede de segurança."""

    def test_threshold_de_aceite_degradado_derruba_precisao(self):
        rotulos = carregar_da_fixture(FIXTURE)
        bom = avaliar(rotulos, t_aceita=T_ACEITA_OK, t_rejeita=T_REJEITA_OK)
        # Degradação de propósito: t_aceita bem baixo deixa itens REJEITADOS (score baixo)
        # passarem como 'confirmado' — precisão despenca.
        degradado = avaliar(rotulos, t_aceita=0.10, t_rejeita=T_REJEITA_OK)

        assert bom.precisao >= LIMIAR_APROVACAO
        assert degradado.precisao is not None
        assert degradado.precisao < LIMIAR_APROVACAO
        assert degradado.precisao < bom.precisao

    def test_threshold_de_rejeicao_degradado_derruba_recall(self):
        rotulos = carregar_da_fixture(FIXTURE)
        bom = avaliar(rotulos, t_aceita=T_ACEITA_OK, t_rejeita=T_REJEITA_OK)
        # Degradação de propósito: t_aceita quase inatingível + t_rejeita bem alto empurra a
        # maioria dos itens CONFIRMADOS (score 0,84–0,97) para "rejeitado" — recall despenca.
        # (t_aceita também sobe: `decidir` checa aceite antes de rejeição, então só subir
        # t_rejeita sem mexer em t_aceita não muda nada para score ≥ t_aceita antigo.)
        degradado = avaliar(rotulos, t_aceita=0.99, t_rejeita=0.95)

        assert bom.recall >= LIMIAR_APROVACAO
        assert degradado.recall is not None
        assert degradado.recall < LIMIAR_APROVACAO
        assert degradado.recall < bom.recall
