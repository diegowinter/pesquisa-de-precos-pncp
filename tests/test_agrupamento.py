"""
Agrupamento e menor preço (Fase 9 — prioridade 3 de docs/08_CONVENCOES.md §6): outlier IQR
(`flag_iqr`, etapa 7). Bug silencioso aqui vira preço errado no export — o dano real do projeto.
"""

import pandas as pd

from pesquisa_precos.steps.e7_group import flag_iqr


class TestFlagIqr:
    def test_precos_coerentes_nao_sao_flagados(self):
        precos = pd.Series([100.0, 102.0, 98.0, 101.0, 99.0, 103.0])
        assert not flag_iqr(precos).any()

    def test_outlier_alto_e_flagado(self):
        precos = pd.Series([100.0, 102.0, 98.0, 101.0, 99.0, 100_000.0])
        flags = flag_iqr(precos)
        assert flags.iloc[-1]
        assert not flags.iloc[:-1].any()

    def test_outlier_baixo_e_flagado(self):
        precos = pd.Series([100.0, 102.0, 98.0, 101.0, 99.0, 0.01])
        flags = flag_iqr(precos)
        assert flags.iloc[-1]

    def test_preco_ausente_e_sempre_flagado(self):
        precos = pd.Series([100.0, 102.0, None, 101.0])
        flags = flag_iqr(precos)
        assert flags.iloc[2]

    def test_preco_zero_ou_negativo_e_sempre_flagado(self):
        precos = pd.Series([100.0, 102.0, 0.0, -5.0, 101.0])
        flags = flag_iqr(precos)
        assert flags.iloc[2] and flags.iloc[3]

    def test_fator_maior_e_mais_permissivo(self):
        """Um fator de IQR maior alarga a banda — o mesmo outlier pode deixar de ser flagado
        se o multiplicador for grande o bastante. Property que protege a calibração."""
        precos = pd.Series([100.0, 102.0, 98.0, 101.0, 99.0, 400.0])
        apertado = flag_iqr(precos, fator=1.0)
        frouxo = flag_iqr(precos, fator=500.0)
        assert apertado.iloc[-1]
        assert not frouxo.iloc[-1]

    def test_lista_toda_identica_sem_flag(self):
        """IQR=0 quando todos os preços são iguais — não pode dividir por zero nem flagar
        tudo à toa."""
        precos = pd.Series([50.0] * 10)
        assert not flag_iqr(precos).any()

    def test_grupo_por_codigo_isola_a_faixa(self):
        """`flag_iqr` é usado via `groupby('codigo').transform(...)` na etapa 7 — um preço
        alto num código não pode contaminar a faixa de outro código."""
        df = pd.DataFrame({
            "codigo": ["A"] * 5 + ["B"] * 5,
            "preco_unitario": [10.0, 11.0, 9.0, 10.5, 9.5,      # código A: coerente
                               1000.0, 1100.0, 900.0, 1050.0, 950.0],  # código B: outra faixa
        })
        flags = df.groupby("codigo")["preco_unitario"].transform(flag_iqr, 3.0)
        assert not flags.any()  # cada faixa é coerente DENTRO do próprio código
