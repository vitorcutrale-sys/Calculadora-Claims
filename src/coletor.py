"""
Orquestrador da coleta de índices.

Estratégia:
- Garante schema e metadados das séries.
- Para cada série, decide entre:
    * carga inicial completa  (se nunca foi baixada)
    * carga incremental       (a partir da última data conhecida)
- Faz upsert de tudo em uma única transação por série.
- Loga cada execução na tabela `coletas_log`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from pathlib import Path
from typing import Optional

from . import bacen, cjf, config, db, juros, tjsp

logger = logging.getLogger(__name__)


def _data_inicio_para_incremental(ultima_data_iso: str) -> str:
    """
    Dada a última data já gravada, retorna a data de início (BR) para o
    próximo pull. Retrocedemos 7 dias para reapanhar eventuais revisões
    que o BACEN aplica retroativamente.
    """
    ultima = datetime.strptime(ultima_data_iso, "%Y-%m-%d").date()
    retroagido = ultima - timedelta(days=7)
    return retroagido.strftime("%d/%m/%Y")


def coletar(modo: str = "auto") -> dict:
    """
    Executa a coleta.

    modo:
        "auto"         : incremental se já existe histórico, senão completo
        "completo"     : força recarga histórica de todas as séries
        "incremental"  : força incremental (erro se não há histórico)

    Retorna um dicionário com estatísticas.
    """
    db.inicializar_schema()

    estatisticas = {
        "series_processadas": 0,
        "series_com_erro": 0,
        "registros_inseridos": 0,
        "erros": [],
    }

    with db.conectar() as conn:
        db.sincronizar_todas_series(conn)
        log_id = db.iniciar_log_coleta(conn)

    # Cada série em sua própria transação — uma falha isolada não derruba
    # as demais.
    for codigo, meta in config.SERIES.items():
        nome = meta["nome"]
        try:
            with db.conectar() as conn:
                ultima = db.ultima_data(conn, codigo)

                if modo == "completo" or (modo == "auto" and ultima is None):
                    data_inicio_br = config.DATA_HISTORICO_INICIAL
                    logger.info(
                        "Série %d (%s): carga COMPLETA a partir de %s.",
                        codigo, nome, data_inicio_br,
                    )
                else:
                    if ultima is None:
                        raise RuntimeError(
                            f"Série {codigo} sem histórico — execute em modo "
                            f"'auto' ou 'completo' primeiro."
                        )
                    data_inicio_br = _data_inicio_para_incremental(ultima)
                    logger.info(
                        "Série %d (%s): carga INCREMENTAL a partir de %s "
                        "(última data: %s).",
                        codigo, nome, data_inicio_br, ultima,
                    )

                observacoes = bacen.baixar_serie(codigo, data_inicio_br)
                inseridas, _ = db.upsert_observacoes(conn, codigo, observacoes)

                estatisticas["series_processadas"] += 1
                estatisticas["registros_inseridos"] += inseridas
                logger.info(
                    "Série %d (%s): %d registros afetados.",
                    codigo, nome, inseridas,
                )
        except Exception as e:
            logger.exception("Falha ao processar série %d (%s).", codigo, nome)
            estatisticas["series_com_erro"] += 1
            estatisticas["erros"].append(f"{codigo} ({nome}): {e}")

    # Status final
    if estatisticas["series_com_erro"] == 0:
        status = "success"
    elif estatisticas["series_processadas"] > 0:
        status = "partial"
    else:
        status = "error"

    mensagem = (
        f"Séries OK: {estatisticas['series_processadas']}, "
        f"com erro: {estatisticas['series_com_erro']}, "
        f"registros: {estatisticas['registros_inseridos']}."
    )
    if estatisticas["erros"]:
        mensagem += " Erros: " + " | ".join(estatisticas["erros"])

    with db.conectar() as conn:
        db.finalizar_log_coleta(
            conn,
            log_id=log_id,
            status=status,
            series_processadas=estatisticas["series_processadas"],
            registros_inseridos=estatisticas["registros_inseridos"],
            mensagem=mensagem,
        )

    estatisticas["status"] = status
    estatisticas["mensagem"] = mensagem
    return estatisticas


# ------------------------------------------------------------------ #
# Coleta TJSP                                                         #
# ------------------------------------------------------------------ #
def coletar_tjsp() -> dict:
    """
    Baixa cada tabela TJSP configurada em config.TJSP_TABELAS e
    atualiza o banco quando há nova versão do PDF (hash mudou).

    Retorna estatísticas: tabelas processadas, novas versões, fatores
    atualizados, erros.
    """
    db.inicializar_schema()

    estatisticas = {
        "tabelas_processadas": 0,
        "tabelas_novas_versoes": 0,
        "tabelas_inalteradas": 0,
        "fatores_atualizados": 0,
        "tabelas_com_erro": 0,
        "erros": [],
    }

    for chave, meta in config.TJSP_TABELAS.items():
        try:
            with db.conectar() as conn:
                sha_anterior = db.ultimo_sha256_tjsp(conn, chave)

            resultado = tjsp.coletar_tabela(
                tabela_chave=chave,
                codigo_arquivo=meta["codigo"],
                sha256_anterior=sha_anterior,
            )

            estatisticas["tabelas_processadas"] += 1

            if not resultado.mudou:
                estatisticas["tabelas_inalteradas"] += 1
                logger.info(
                    "TJSP %s: sem mudança desde a última coleta.", chave
                )
                continue

            # Nova versão: persiste publicação + fatores
            with db.conectar() as conn:
                pub_id = db.registrar_publicacao_tjsp(
                    conn,
                    tabela_chave=chave,
                    codigo_arquivo=meta["codigo"],
                    sha256=resultado.sha256,
                    caminho_arquivo=str(resultado.caminho_arquivo),
                )
                afetadas = db.upsert_fatores_tjsp(
                    conn,
                    tabela_chave=chave,
                    publicacao_id=pub_id,
                    fatores=resultado.fatores,
                )

            estatisticas["tabelas_novas_versoes"] += 1
            estatisticas["fatores_atualizados"] += afetadas
            logger.info(
                "TJSP %s: %d fatores atualizados (publicação %d).",
                chave, afetadas, pub_id,
            )

        except Exception as e:
            logger.exception("Falha ao processar tabela TJSP %s.", chave)
            estatisticas["tabelas_com_erro"] += 1
            estatisticas["erros"].append(f"{chave}: {e}")

    return estatisticas


# ------------------------------------------------------------------ #
# Coleta CJF                                                          #
# ------------------------------------------------------------------ #
def coletar_cjf(mes: int = 0, ano: int = 0) -> dict:
    """
    Baixa cada tabela CJF configurada em config.CJF_TABELAS para o
    mês/ano informado (padrão: mês e ano correntes).

    Usa SHA-256 para detectar mudanças e evitar reprocessamento.

    Retorna estatísticas: tabelas processadas, novas versões, fatores
    atualizados, erros.
    """
    db.inicializar_schema()

    hoje = date.today()
    mes_ref = mes or hoje.month
    ano_ref = ano or hoje.year

    estatisticas = {
        "mes_referencia": mes_ref,
        "ano_referencia": ano_ref,
        "tabelas_processadas": 0,
        "tabelas_novas_versoes": 0,
        "tabelas_inalteradas": 0,
        "fatores_atualizados": 0,
        "tabelas_com_erro": 0,
        "erros": [],
    }

    for chave, meta in config.CJF_TABELAS.items():
        try:
            with db.conectar() as conn:
                sha_anterior = db.ultimo_sha256_cjf(conn, chave)

            resultado = cjf.coletar_tabela(
                tabela_chave=chave,
                tipo_tabela=meta["tipo_tabela"],
                seq_encadeamento=meta["seq_encadeamento"],
                mes_referencia=mes_ref,
                ano_referencia=ano_ref,
                sha256_anterior=sha_anterior,
            )

            estatisticas["tabelas_processadas"] += 1

            if not resultado.mudou:
                estatisticas["tabelas_inalteradas"] += 1
                logger.info("CJF %s: sem mudança desde a última coleta.", chave)
                continue

            with db.conectar() as conn:
                pub_id = db.registrar_publicacao_cjf(
                    conn,
                    tabela_chave=chave,
                    tipo_tabela=resultado.tipo_tabela,
                    seq_encadeamento=resultado.seq_encadeamento,
                    mes_referencia=resultado.mes_referencia,
                    ano_referencia=resultado.ano_referencia,
                    sha256=resultado.sha256,
                    caminho_arquivo=str(resultado.caminho_arquivo),
                )
                afetadas = db.upsert_fatores_cjf(
                    conn,
                    tabela_chave=chave,
                    publicacao_id=pub_id,
                    fatores=resultado.fatores,
                )

            estatisticas["tabelas_novas_versoes"] += 1
            estatisticas["fatores_atualizados"] += afetadas
            logger.info(
                "CJF %s: %d fatores atualizados (publicação %d).",
                chave, afetadas, pub_id,
            )

        except Exception as e:
            logger.exception("Falha ao processar tabela CJF %s.", chave)
            estatisticas["tabelas_com_erro"] += 1
            estatisticas["erros"].append(f"{chave}: {e}")

    return estatisticas


# ------------------------------------------------------------------ #
# Importação de Juros de Mora (estática / única)                      #
# ------------------------------------------------------------------ #
def importar_juros(origem_dir: Optional[Path] = None) -> dict:
    """
    Importa as tabelas de juros de mora configuradas em
    config.JUROS_TABELAS a partir de PDFs locais.

    `origem_dir` é a pasta onde estão os PDFs de origem. Se None, usa
    config.JUROS_FONTE_DIR. Cada PDF é arquivado em referencias/juros/
    com hash no nome; SHA-256 evita reimportar versão idêntica.

    Esta importação é ESTÁTICA (sem coleta periódica): os juros de mora
    deixaram de ser aplicados em 09/12/2021 (EC 113/2021). Por isso ela
    NÃO faz parte de coletar_tudo().
    """
    db.inicializar_schema()

    origem = Path(origem_dir) if origem_dir else config.JUROS_FONTE_DIR

    estatisticas = {
        "origem": str(origem),
        "tabelas_processadas": 0,
        "tabelas_novas_versoes": 0,
        "tabelas_inalteradas": 0,
        "taxas_importadas": 0,
        "tabelas_com_erro": 0,
        "erros": [],
    }

    for chave, meta in config.JUROS_TABELAS.items():
        try:
            with db.conectar() as conn:
                sha_anterior = db.ultimo_sha256_juros(conn, chave)

            resultado = juros.importar_tabela(
                tabela_chave=chave,
                arquivo_origem=origem / meta["arquivo_origem"],
                sha256_anterior=sha_anterior,
            )

            estatisticas["tabelas_processadas"] += 1

            if not resultado.mudou:
                estatisticas["tabelas_inalteradas"] += 1
                logger.info("JUROS %s: sem mudança desde a última importação.", chave)
                continue

            with db.conectar() as conn:
                pub_id = db.registrar_publicacao_juros(
                    conn,
                    tabela_chave=chave,
                    descricao=meta["descricao"],
                    data_referencia=meta["data_referencia"],
                    sha256=resultado.sha256,
                    caminho_arquivo=str(resultado.caminho_arquivo),
                )
                afetadas = db.upsert_fatores_juros(
                    conn,
                    tabela_chave=chave,
                    publicacao_id=pub_id,
                    taxas=resultado.taxas,
                )

            estatisticas["tabelas_novas_versoes"] += 1
            estatisticas["taxas_importadas"] += afetadas
            logger.info(
                "JUROS %s: %d taxas importadas (publicação %d).",
                chave, afetadas, pub_id,
            )

        except Exception as e:
            logger.exception("Falha ao importar tabela de juros %s.", chave)
            estatisticas["tabelas_com_erro"] += 1
            estatisticas["erros"].append(f"{chave}: {e}")

    return estatisticas


# ------------------------------------------------------------------ #
# Coleta unificada (BACEN + TJSP)                                     #
# ------------------------------------------------------------------ #
def coletar_tudo(modo_bacen: str = "auto") -> dict:
    """
    Roda a coleta completa: BACEN seguido de TJSP.

    Retorna estatísticas agregadas de ambas as fontes. Falha em uma
    não interrompe a outra.
    """
    logger.info("--- Coleta BACEN ---")
    try:
        bacen_stats = coletar(modo=modo_bacen)
    except Exception as e:
        logger.exception("Erro fatal na coleta BACEN.")
        bacen_stats = {
            "status": "error",
            "mensagem": str(e),
            "series_processadas": 0,
            "registros_inseridos": 0,
        }

    logger.info("--- Coleta TJSP ---")
    try:
        tjsp_stats = coletar_tjsp()
    except Exception as e:
        logger.exception("Erro fatal na coleta TJSP.")
        tjsp_stats = {
            "tabelas_processadas": 0,
            "tabelas_novas_versoes": 0,
            "fatores_atualizados": 0,
            "tabelas_com_erro": 1,
            "erros": [str(e)],
        }

    logger.info("--- Coleta CJF ---")
    try:
        cjf_stats = coletar_cjf()
    except Exception as e:
        logger.exception("Erro fatal na coleta CJF.")
        cjf_stats = {
            "tabelas_processadas": 0,
            "tabelas_novas_versoes": 0,
            "fatores_atualizados": 0,
            "tabelas_com_erro": 1,
            "erros": [str(e)],
        }

    return {
        "bacen": bacen_stats,
        "tjsp": tjsp_stats,
        "cjf": cjf_stats,
    }
