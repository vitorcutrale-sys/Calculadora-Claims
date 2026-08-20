"""
Entry point da coleta mensal de índices (BACEN + TJSP + CJF).

Uso:
    python coletar_indices.py                          # auto (BACEN + TJSP + CJF)
    python coletar_indices.py --completo               # força recarga histórica BACEN
    python coletar_indices.py --incremental            # força incremental BACEN
    python coletar_indices.py --so-bacen               # roda apenas BACEN
    python coletar_indices.py --so-tjsp                # roda apenas TJSP
    python coletar_indices.py --so-cjf                 # roda apenas CJF
    python coletar_indices.py --so-cjf --mes 5 --ano 2025  # CJF para mês/ano específico
    python coletar_indices.py --so-irpf                 # coleta anual da tabela do IRPF
    python coletar_indices.py --so-irpf --ano 2025       # IRPF de um ano específico
    python coletar_indices.py --importar-juros          # importa tabelas de juros de mora (estático)
    python coletar_indices.py --importar-juros --origem "C:\\caminho\\dos\\pdfs"  # origem custom
    python coletar_indices.py --resumo                 # apenas imprime resumo do banco

O modo padrão executa BACEN (auto) seguido de TJSP e CJF. Falha em uma
fonte não interrompe as demais.

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
        linhas_cjf = db.resumo_cjf(conn)
        linhas_juros = db.resumo_juros(conn)
        linhas_irpf = db.resumo_irpf(conn)

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

    if linhas_cjf:
        print("\n=== Resumo das Tabelas CJF ===")
        print(f"{'Tabela':<16} | {'Fatores':>8} | {'1º ano':>7} | {'Últ. ano':>8}")
        print("-" * 52)
        for r in linhas_cjf:
            print(
                f"{r['tabela_chave']:<16} | {r['n_fatores']:>8} | "
                f"{r['primeiro_ano']:>7} | {r['ultimo_ano']:>8}"
            )

    if linhas_juros:
        print("\n=== Resumo das Tabelas de Juros de Mora ===")
        print(f"{'Tabela':<22} | {'Taxas':>8} | {'1º ano':>7} | {'Últ. ano':>8}")
        print("-" * 56)
        for r in linhas_juros:
            print(
                f"{r['tabela_chave']:<22} | {r['n_fatores']:>8} | "
                f"{r['primeiro_ano']:>7} | {r['ultimo_ano']:>8}"
            )

    if linhas_irpf:
        print("\n=== Tabela Progressiva Mensal do IRPF (por vigência) ===")
        print(f"{'Vigência':<24} | {'Faixas':>6} | {'Isenção até (R$)':>17}")
        print("-" * 54)
        for r in linhas_irpf:
            periodo = f"{r['vigencia_inicio']} a {r['vigencia_fim'] or 'atual'}"
            print(
                f"{periodo:<24} | {r['n_faixas']:>6} | "
                f"{r['isencao_ate']:>17,.2f}"
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
    grupo_fonte.add_argument(
        "--so-cjf",
        action="store_true",
        help="Roda apenas a coleta do CJF (Justiça Federal).",
    )
    grupo_fonte.add_argument(
        "--importar-juros",
        action="store_true",
        help="Importa as tabelas de juros de mora (estático, a partir de PDFs locais).",
    )
    grupo_fonte.add_argument(
        "--so-irpf",
        action="store_true",
        help="Roda apenas a coleta anual da Tabela Progressiva Mensal do IRPF.",
    )

    parser.add_argument(
        "--mes",
        type=int,
        default=0,
        metavar="MM",
        help="Mês de referência para coleta CJF (padrão: mês corrente).",
    )
    parser.add_argument(
        "--ano",
        type=int,
        default=0,
        metavar="AAAA",
        help="Ano de referência para coleta CJF (padrão: ano corrente).",
    )
    parser.add_argument(
        "--origem",
        type=str,
        default=None,
        metavar="DIR",
        help=(
            "Pasta de origem dos PDFs para --importar-juros "
            "(padrão: referencias/juros/)."
        ),
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
        elif args.so_cjf:
            logger.info(
                "Modo: apenas CJF (mes=%s, ano=%s)",
                args.mes or "corrente", args.ano or "corrente",
            )
            estat_cjf = coletor.coletar_cjf(mes=args.mes, ano=args.ano)
            logger.info(
                "CJF: %d tabelas, %d novas versões, %d fatores atualizados.",
                estat_cjf["tabelas_processadas"],
                estat_cjf["tabelas_novas_versoes"],
                estat_cjf["fatores_atualizados"],
            )
            if estat_cjf["tabelas_com_erro"] > 0:
                exit_code = 1
        elif args.so_irpf:
            logger.info("Modo: apenas IRPF (ano=%s)", args.ano or "corrente")
            estat_irpf = coletor.coletar_irpf(ano=args.ano)
            if estat_irpf["pagina_inalterada"]:
                logger.info(
                    "IRPF %d: página inalterada, nada a fazer.",
                    estat_irpf["ano_pagina"],
                )
            else:
                logger.info(
                    "IRPF %d: %d tabela(s), %d faixa(s) atualizada(s).",
                    estat_irpf["ano_pagina"],
                    estat_irpf["tabelas_encontradas"],
                    estat_irpf["faixas_atualizadas"],
                )
            if estat_irpf["erros"]:
                for err in estat_irpf["erros"]:
                    logger.error("IRPF erro: %s", err)
                exit_code = 1
        elif args.importar_juros:
            logger.info(
                "Modo: importação de juros de mora (origem=%s)",
                args.origem or "referencias/juros/",
            )
            estat_juros = coletor.importar_juros(origem_dir=args.origem)
            logger.info(
                "JUROS: %d tabelas, %d novas versões, %d taxas importadas (origem: %s).",
                estat_juros["tabelas_processadas"],
                estat_juros["tabelas_novas_versoes"],
                estat_juros["taxas_importadas"],
                estat_juros["origem"],
            )
            if estat_juros["tabelas_com_erro"] > 0:
                for err in estat_juros["erros"]:
                    logger.error("JUROS erro: %s", err)
                exit_code = 1
        else:
            logger.info("Modo: BACEN (%s) + TJSP + CJF", modo_bacen)
            estat_full = coletor.coletar_tudo(modo_bacen=modo_bacen)
            b = estat_full["bacen"]
            t = estat_full["tjsp"]
            c = estat_full["cjf"]
            logger.info("BACEN status: %s | %s", b.get("status"), b.get("mensagem"))
            logger.info(
                "TJSP: %d tabelas, %d novas versões, %d fatores atualizados.",
                t["tabelas_processadas"],
                t["tabelas_novas_versoes"],
                t["fatores_atualizados"],
            )
            logger.info(
                "CJF: %d tabelas, %d novas versões, %d fatores atualizados.",
                c["tabelas_processadas"],
                c["tabelas_novas_versoes"],
                c["fatores_atualizados"],
            )
            if b.get("status") != "success" or t["tabelas_com_erro"] > 0 or c["tabelas_com_erro"] > 0:
                exit_code = 1
    except Exception:
        logger.exception("Erro fatal na coleta.")
        return 2

    logger.info("=== Fim da coleta ===")
    imprimir_resumo()
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
