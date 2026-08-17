"""
Normalização de texto e `texto_hash` — prioridade máxima em docs/08_CONVENCOES.md §6.

Esta função é calculada na ingestão (etapa 2 / migração m07) e consultada na classificação
(etapa 3). Uma diferença mínima entre as duas pontas invalida o dedup permanente e manda 320
mil textos já pagos de volta ao LLM. É o teste que protege o dinheiro.
"""

from pesquisa_precos.core.textos import normalizar_texto, texto_hash


class TestNormalizar:
    def test_minusculo_e_espacos_colapsados(self):
        assert normalizar_texto("  PISTOLA   9MM  ") == "pistola 9mm"

    def test_remove_acento(self):
        assert normalizar_texto("Agente Lacrimogêneo") == normalizar_texto("agente lacrimogeneo")

    def test_cedilha_e_til(self):
        assert normalizar_texto("MUNIÇÃO") == "municao"

    def test_vazio_e_none(self):
        assert normalizar_texto(None) == ""
        assert normalizar_texto("") == ""
        assert normalizar_texto("   ") == ""

    def test_quebra_de_linha_conta_como_espaco(self):
        assert normalizar_texto("colete\nbalístico") == "colete balistico"


class TestTextoHash:
    def test_estavel(self):
        """Property test do §6: mesma entrada, mesmo hash, sempre."""
        assert texto_hash("Bota segurança", "Par") == texto_hash("Bota segurança", "Par")

    def test_sensivel_a_unidade(self):
        """(descrição, unidade) é a chave: a mesma descrição em unidade diferente é outro
        texto para o classificador, então não pode colidir."""
        assert texto_hash("Cabo de rede", "Metro") != texto_hash("Cabo de rede", "Unidade")

    def test_unidade_ausente_equivale_a_vazia(self):
        assert texto_hash("Drone") == texto_hash("Drone", "") == texto_hash("Drone", None)

    def test_acento_e_caixa_nao_separam(self):
        assert texto_hash("MUNIÇÃO 9MM", "UN") == texto_hash("municao 9mm", "un")

    def test_pipe_na_descricao_e_ambiguidade_conhecida(self):
        """AMBIGUIDADE ACEITA, registrada de propósito.

        A fórmula é `sha1(norm(descricao) || '|' || norm(unidade))`, normativa em
        docs/02_SCHEMA.md §4. Um '|' dentro da descrição desloca a fronteira: ("a|b", "c") e
        ("a", "b|c") produzem o mesmo hash. Na prática isso não acontece — a unidade do PNCP
        é um vocabulário curto ('Unidade', 'Par', 'Metro') e nunca contém '|'.

        O teste afirma o comportamento REAL em vez de fingir que ele não existe: se alguém
        um dia trocar o separador (o que resolveria a ambiguidade), este teste falha e a
        pessoa descobre, ANTES de rodar, que acabou de invalidar 320 mil hashes já pagos.
        """
        assert texto_hash("a|b", "c") == texto_hash("a", "b|c")

    def test_formato_sha1(self):
        h = texto_hash("qualquer coisa", "un")
        assert len(h) == 40 and all(c in "0123456789abcdef" for c in h)
