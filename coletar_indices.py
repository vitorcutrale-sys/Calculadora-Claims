"""
Entry point da coleta diária de índices (BACEN + TJSP).

Uso:
    python coletar_indices.py                   # auto (BACEN + TJSP)
    python coletar_indices.py --completo        # força recarga histórica BACEN
    python coletar_indices.py --incremental     # força incremental BACEN
    python coletar_indices.py --so-bacen        # roda apenas BACEN
    python coletar_indices.py --so-tjsp         # roda apenas TJSP
    python coletar_indices.py --resumo          # apenas imprime resumo do banco

O modo padrão executa BACEN (auto) seguido de TJSP. Falha em uma
fonte não interrompe a outra.

Logs são gravados em /logs/coleta_AAAA-MM-DD.log e também impressos
no console.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from src import coletor, config, db


def configurar_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.LOG_DIR / f"coleta_{date.today().isoformat()}.log"

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def imprimir_resumo() -> None:
    db.inicializar_schema()
    with db.conectar() as conn:
        db.sincronizar_todas_series(conn)
        linhas_bacen = db.resumo(conn)
        linhas_tjsp = db.resumo_tjsp(conn)

    print("\n=== Resumo do banco de índices (BACEN) ===")
    print(
        f"{'Cód':>6} | {'Nome':<28} | {'Obs.':>8} | "
        f"{'1ª data':<10} | {'Última':<10}"
    )
    print("-" * 76)
    for r in linhas_bacen:
        print(
            f"{r['codigo']:>6} | {r['nome']:<28} | {r['n_obs']:>8} | "
            f"{r['primeira'] or '-':<10} | {r['ultima'] or '-':<10}"
        )

    if linhas_tjsp:
        print("\n=== Resumo das Tabelas TJSP ===")
        print(f"{'Tabela':<24} | {'Fatores':>8} | {'1º ano':>7} | {'Últ. ano':>8}")
        print("-" * 60)
        for r in linhas_tjsp:
            print(
                f"{r['tabela_chave']:<24} | {r['n_fatores']:>8} | "
                f"{r['primeiro_ano']:>7} | {r['ultimo_ano']:>8}"
            )

    print(f"\nBanco: {config.DB_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Coleta diária de índices econômicos (BACEN + TJSP)."
    )
    grupo_modo = parser.add_mutually_exclusive_group()
    grupo_modo.add_argument(
        "--completo",
        action="store_true",
        help="Força recarga histórica completa do BACEN.",
    )
    grupo_modo.add_argument(
        "--incremental",
        action="store_true",
        help="Força carga incremental do BACEN.",
    )
    grupo_modo.add_argument(
        "--resumo",
        action="store_true",
        help="Apenas imprime resumo do banco e sai.",
    )

    grupo_fonte = parser.add_mutually_exclusive_group()
    grupo_fonte.add_argument(
        "--so-bacen",
        action="store_true",
        help="Roda apenas a coleta do BACEN.",
    )
    grupo_fonte.add_argument(
        "--so-tjsp",
        action="store_true",
        help="Roda apenas a coleta do TJSP.",
    )

    args = parser.parse_args()

    configurar_logging()
    logger = logging.getLogger("coletar_indices")

    if args.resumo:
        imprimir_resumo()
        return 0

    if args.completo:
        modo_bacen = "completo"
    elif args.incremental:
        modo_bacen = "incremental"
    else:
        modo_bacen = "auto"

    logger.info("=== Início da coleta ===")

    exit_code = 0
    try:
        if args.so_tjsp:
            logger.info("Modo: apenas TJSP")
            estat_tjsp = coletor.coletar_tjsp()
            logger.info(
                "TJSP: %d tabelas processadas, %d novas versões, %d fatores.",
                estat_tjsp["tabelas_processadas"],
                estat_tjsp["tabelas_novas_versoes"],
                estat_tjsp["fatores_atualizados"],
            )
            if estat_tjsp["tabelas_com_erro"] > 0:
                exit_code = 1
        elif args.so_bacen:
            logger.info("Modo: apenas BACEN (%s)", modo_bacen)
            estat = coletor.coletar(modo=modo_bacen)
            logger.info("BACEN status: %s | %s", estat["status"], estat["mensagem"])
            if estat["status"] != "success":
                exit_code = 1
        else:
            logger.info("Modo: BACEN (%s) + TJSP", modo_bacen)
            estat_full = coletor.coletar_tudo(modo_bacen=modo_bacen)
            b = estat_full["bacen"]
            t = estat_full["tjsp"]
            logger.info("BACEN status: %s | %s", b.get("status"), b.get("mensagem"))
            logger.info(
                "TJSP: %d tabelas, %d novas versões, %d fatores atualizados.",
                t["tabelas_processadas"],
                t["tabelas_novas_versoes"],
                t["fatores_atualizados"],
            )
            if b.get("status") != "success" or t["tabelas_com_erro"] > 0:
                exit_code = 1
    except Exception:
        logger.exception("Erro fatal na coleta.")
        return 2

    logger.info("=== Fim da coleta ===")
    imprimir_resumo()
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
