"""
Bench multi-modelo (OpenRouter) — bateria rotulada à mão, SEM carregar nenhum CSV.

Usa o caminho REAL de produção: Curador.classificar_categoria (mesmo prompt, mesmo
parsing, filtra ids inválidos). Roda cada modelo na bateria e reporta acerto + tempo.

Uso:
    uv run python scratchpad/bench_openrouter.py
    uv run python scratchpad/bench_openrouter.py --concurrency 6 --modelos "a,b,c"
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, ".")
from scripts.config import carregar_config  # noqa: E402
from scripts.llm_curador import Curador  # noqa: E402

# (descricao, unidade, categoria_esperada)  — "" = deve dar lista vazia (acessório/fora)
CASOS = [
    ("PISTOLA .40 GLOCK G22C", "UN", "arma_fogo"),
    ("FUZIL DE ASSALTO 5.56MM", "UN", "arma_fogo"),
    ("CARREGADOR (MAGAZINE) PARA PISTOLA .40", "UN", ""),
    ("COLDRE EM POLIMERO PARA PISTOLA", "UN", ""),
    ("MIRA RED DOT PARA FUZIL", "UN", ""),
    ("MUNICAO 9MM LUGER EOPE", "UN", "municao"),
    ("ESTOJO VAZIO CALIBRE .40 (RECARGA)", "UN", ""),
    ("SPRAY DE PIMENTA OC 70G", "UN", "arma_nao_letal"),
    ("BASTAO RETRATIL TONFA POLICIAL", "UN", "arma_nao_letal"),
    ("CARTUCHO/RECARGA PARA TASER X26", "UN", ""),
    ("COLETE BALISTICO NIVEL IIIA", "UN", "protecao_balistica"),
    ("COLETE TATICO MODULAR (SEM MENCAO BALISTICA)", "UN", "vestuario_operacional"),
    ("CAPA/PORTA-COLETE EM CORDURA", "UN", "vestuario_operacional"),
    ("CAPACETE BALISTICO NIVEL IIIA", "UN", "protecao_balistica"),
    ("CAPACETE DE MOTOCICLISTA", "UN", ""),
    ("NOTEBOOK CORE I7 16GB SSD", "UN", "equip_ti"),
    ("TONER PARA IMPRESSORA LASER", "UN", ""),
    ("MONITOR LED 24 POLEGADAS", "UN", ""),
    ("CAMERA DE SEGURANCA CFTV IP", "UN", ""),
    ("RADIO COMUNICADOR HT DIGITAL VHF", "UN", "equip_comunicacao"),
    ("BATERIA PARA RADIO HT", "UN", ""),
    ("CAMINHONETE 4X4 CABINE DUPLA DIESEL", "UN", "viatura"),
    ("PNEU 265/65 R17 PARA VIATURA", "UN", ""),
    ("MOTOCICLETA 300CC POLICIAL", "UN", "viatura"),
    ("BICICLETA ARO 29 PARA CICLOPATRULHA", "UN", "bicicleta"),
    ("DRONE MULTIRROTOR COM CAMERA TERMICA 4K", "UN", "drone_rpas"),
    ("HELICE DE REPOSICAO PARA DRONE", "UN", ""),
    ("ORGANIZACAO DE CONCURSO PUBLICO", "SERVICO", "servico_selecao"),
    ("MONITORAMENTO ELETRONICO POR CFTV", "SERVICO", "servico_seguranca"),
    ("INSTALACAO DE CAMERAS DE SEGURANCA", "SERVICO", ""),
]

MODELOS_DEFAULT = [
    "inclusionai/ling-2.6-flash",
    "mistralai/mistral-nemo",
    "meta-llama/llama-3.1-8b-instruct",
    "nex-agi/nex-n2-mini",
    "openai/gpt-oss-120b",
]


def roda_modelo(cfg, modelo, concurrency):
    cur = Curador(
        model=modelo,
        base_url=cfg["openrouter_base_url"],
        api_key=cfg["openrouter_api_key"],
        max_retries=4,
        reasoning={"enabled": False},  # desliga think em quem suporta (OpenRouter)
    )
    resultados = [None] * len(CASOS)

    def um(i):
        desc, uni, esp = CASOS[i]
        try:
            res = cur.classificar_categoria(desc, uni)
        except Exception as e:  # noqa: BLE001
            return i, [], f"EXC:{str(e)[:60]}", False
        cats = res["categorias"]
        ok = (not cats) if esp == "" else (esp in cats)
        return i, cats, res.get("confianca", ""), ok

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for i, cats, conf, ok in ex.map(um, range(len(CASOS))):
            resultados[i] = (cats, conf, ok)
    dt = time.time() - t0
    acertos = sum(r[2] for r in resultados)
    return resultados, acertos, dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--modelos", default=None, help="lista separada por vírgula (sobrescreve o default)")
    ap.add_argument("--detalhe", action="store_true", help="imprime cada caso errado")
    args = ap.parse_args()

    cfg = carregar_config()
    if not cfg["openrouter_api_key"]:
        raise SystemExit("Falta OPENAI_API_KEY no .env (provedor openrouter).")
    modelos = [m.strip() for m in args.modelos.split(",")] if args.modelos else MODELOS_DEFAULT

    print(f"bateria: {len(CASOS)} casos | concurrency: {args.concurrency}\n")
    resumo = []
    for m in modelos:
        print(f"=== {m} ===")
        try:
            resultados, acertos, dt = roda_modelo(cfg, m, args.concurrency)
        except Exception as e:  # noqa: BLE001
            print(f"  FALHOU: {str(e)[:200]}\n")
            resumo.append((m, None, None))
            continue
        for i, (cats, conf, ok) in enumerate(resultados):
            if not ok or args.detalhe:
                desc, uni, esp = CASOS[i]
                mark = "OK " if ok else "XXX"
                print(f"  {mark} esp={(esp or '(vazio)'):20} got={cats} [{conf}] | {desc[:40]}")
        pct = 100 * acertos / len(CASOS)
        print(f"  -> {acertos}/{len(CASOS)} ({pct:.0f}%) em {dt:.0f}s\n")
        resumo.append((m, pct, dt))

    print("=" * 50)
    print(f"{'MODELO':40} {'ACERTO':>7} {'TEMPO':>7}")
    for m, pct, dt in sorted(resumo, key=lambda x: (x[1] is None, -(x[1] or 0))):
        if pct is None:
            print(f"{m:40} {'FALHOU':>7}")
        else:
            print(f"{m:40} {pct:6.0f}% {dt:6.0f}s")


if __name__ == "__main__":
    main()
