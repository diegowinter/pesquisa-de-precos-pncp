"""
Backup do PostgreSQL (Fase 9, item 4). Testa as partes puras (parse de URL, checagem de
integridade) e o disparo do `pg_dump` com o subprocesso MOCKADO — sem exigir Postgres real
no ambiente de CI/dev do agente (CLAUDE.md: Claude não roda infraestrutura).
"""

from unittest.mock import patch

import pytest

from ferramentas.backup import (
    ASSINATURA_PGDUMP,
    nome_arquivo,
    rodar_pg_dump,
    url_para_args_pg_dump,
    verificar_integridade,
)


class TestUrlParaArgsPgDump:
    def test_extrai_host_porta_usuario_banco(self):
        args = url_para_args_pg_dump(
            "postgresql+psycopg://postgres:segredo@localhost:5432/pesquisa_precos")
        assert args == ["--host", "localhost", "--port", "5432",
                        "--username", "postgres", "pesquisa_precos"]

    def test_senha_nunca_aparece_nos_args(self):
        args = url_para_args_pg_dump(
            "postgresql+psycopg://postgres:segredo-super-secreto@localhost:5432/db")
        assert "segredo-super-secreto" not in " ".join(args)

    def test_sem_banco_no_path_usa_default(self):
        args = url_para_args_pg_dump("postgresql+psycopg://postgres@localhost:5432/")
        assert args[-1] == "pesquisa_precos"


class TestNomeArquivo:
    def test_formato_datado(self):
        from datetime import datetime
        nome = nome_arquivo(datetime(2026, 8, 17, 14, 30, 5))
        assert nome == "pesquisa_precos_20260817_143005.dump"


class TestVerificarIntegridade:
    def test_arquivo_inexistente_falha(self, tmp_path):
        ok, msg = verificar_integridade(tmp_path / "nao_existe.dump")
        assert ok is False and "não existe" in msg

    def test_arquivo_vazio_falha(self, tmp_path):
        arquivo = tmp_path / "vazio.dump"
        arquivo.write_bytes(b"")
        ok, msg = verificar_integridade(arquivo)
        assert ok is False and "vazio" in msg

    def test_sem_assinatura_pgdump_falha(self, tmp_path):
        arquivo = tmp_path / "invalido.dump"
        arquivo.write_bytes(b"nao e um dump valido, so texto qualquer")
        ok, msg = verificar_integridade(arquivo)
        assert ok is False and "assinatura" in msg

    def test_com_assinatura_pgdump_passa(self, tmp_path):
        arquivo = tmp_path / "valido.dump"
        arquivo.write_bytes(ASSINATURA_PGDUMP + b"\x00\x01\x02resto do dump binario simulado")
        ok, msg = verificar_integridade(arquivo)
        assert ok is True and "assinatura OK" in msg


class TestRodarPgDump:
    def test_chama_pg_dump_com_formato_custom_e_arquivo_datado(self, tmp_path):
        with patch("ferramentas.backup.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            arquivo = rodar_pg_dump(
                "postgresql+psycopg://postgres:senha@localhost:5432/pesquisa_precos",
                tmp_path, executavel="pg_dump")
        comando = mock_run.call_args[0][0]
        assert comando[0] == "pg_dump"
        assert "-Fc" in comando
        assert str(arquivo) in comando
        assert arquivo.parent == tmp_path
        assert arquivo.name.startswith("pesquisa_precos_") and arquivo.name.endswith(".dump")

    def test_senha_vai_via_pgpassword_no_ambiente(self, tmp_path):
        with patch("ferramentas.backup.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            rodar_pg_dump("postgresql+psycopg://postgres:segredo123@localhost:5432/db", tmp_path)
        ambiente = mock_run.call_args.kwargs["env"]
        assert ambiente["PGPASSWORD"] == "segredo123"
        assert "segredo123" not in " ".join(mock_run.call_args[0][0])

    def test_falha_do_pg_dump_levanta_systemexit(self, tmp_path):
        with patch("ferramentas.backup.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "pg_dump: erro: conexão recusada"
            with pytest.raises(SystemExit, match="conexão recusada"):
                rodar_pg_dump("postgresql+psycopg://postgres@localhost:5432/db", tmp_path)
