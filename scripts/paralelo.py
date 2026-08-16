"""
Pool de workers genérico com callbacks (seção 4 do guia).

Extraído do motor paralelo do `curar_catalogo_grupos_paralelo.py` da v1 e generalizado.
Executa `fn(item)` em paralelo (ThreadPoolExecutor) e chama, na thread principal e à
medida que os resultados chegam, `on_result(item, resultado)` ou `on_error(item, exc)`.

Manter os callbacks na thread principal é intencional: quem escreve com `EscritorSeguro`
(um único escritor, sem lock) fica seguro. O retry/backoff de rede (ex.: 429) fica a
cargo do próprio `fn` (o `Curador` já usa max_retries nativo + with_retry).
"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def executar_paralelo(itens, fn, concurrency=8, on_result=None, on_error=None,
                      on_progress=None, max_em_voo=None):
    """
    Roda `fn(item)` para cada item de `itens` em até `concurrency` threads.

    - on_result(item, resultado): chamado (thread principal) para cada sucesso.
    - on_error(item, exc): chamado (thread principal) para cada exceção de `fn`.
      Se ausente, a exceção é re-levantada (falha dura).
    - on_progress(feitos, total): chamado após cada item concluído (total pode ser None
      se `itens` for um iterador sem len()).
    - max_em_voo: teto de futures pendentes (default: concurrency*4) — evita materializar
      um iterador gigante inteiro na memória.

    Retorna o número de itens processados.
    """
    try:
        total = len(itens)
    except TypeError:
        total = None
    it = iter(itens)
    teto = max_em_voo or concurrency * 4
    feitos = 0

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        pendentes = {}

        def _submeter_ate_encher():
            while len(pendentes) < teto:
                try:
                    item = next(it)
                except StopIteration:
                    break
                pendentes[ex.submit(fn, item)] = item

        _submeter_ate_encher()
        while pendentes:
            concluidos, _ = wait(pendentes, return_when=FIRST_COMPLETED)
            for fut in concluidos:
                item = pendentes.pop(fut)
                try:
                    resultado = fut.result()
                    if on_result is not None:
                        on_result(item, resultado)
                except Exception as exc:  # noqa: BLE001
                    if on_error is None:
                        raise
                    on_error(item, exc)
                feitos += 1
                if on_progress is not None:
                    on_progress(feitos, total)
            _submeter_ate_encher()
    return feitos
