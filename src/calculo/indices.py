"""
Leitor de índices em Decimal — Fase 5, fatia 2.

Lê os fatores/taxas já armazenados no claims.db e os devolve SEMPRE em
Decimal, prontos para a aritmética financeira do motor. Nenhuma multiplicação
encadeada de fatores deve usar float (acúmulo de erro em séries longas).

Foco desta etapa: regime R1 (pré-EC 113) do bloco federal de servidores.

  fator_correcao_sicom() : coeficiente de correção monetária da tabela
                           SICOM/CJF (TCM_3 = servidores) para o (ano, mês).
                           Usado apenas para data base <= dez/2021 (R1).
  taxa_juros_acumulada() : taxa de juros de mora ACUMULADA (%) da tabela
                           JUROS_SERVIDOR para um (ano, mês).
  fracao_juros_mora()    : fração de juros de mora entre duas datas,
                           = (taxa(ini) - taxa(fim)) / 100.

Os leitores de Selic (R2) e IPCA (R3), a partir das séries BACEN, entram nas
próximas etapas — cada uma com seu método próprio.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date
from decimal import Decimal

CEM = Decimal("100")


def _d(valor) -> Decimal:
    """Converte um valor lido do banco (REAL/float) para Decimal sem ruído."""
    return Decimal(str(valor))


# ------------------------------------------------------------------ #
# CJF / SICOM — correção monetária (R1)                               #
# ------------------------------------------------------------------ #
def fator_correcao_sicom(
    conn: sqlite3.Connection, tabela_chave: str, quando: date
) -> Decimal:
    """
    Coeficiente de correção monetária da tabela SICOM/CJF para o mês/ano de
    `quando`. Para o bloco de servidores, `tabela_chave` = "TCM_3".

    Levanta ValueError se não houver fator para aquele (ano, mês).
    """
    row = conn.execute(
        "SELECT valor FROM cjf_fatores WHERE tabela_chave = ? AND ano = ? AND mes = ?",
        (tabela_chave, quando.year, quando.month),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"SICOM/CJF '{tabela_chave}' não possui fator para {quando:%Y-%m}."
        )
    return _d(row[0])


# ------------------------------------------------------------------ #
# Juros de Mora (R1)                                                  #
# ------------------------------------------------------------------ #
def taxa_juros_acumulada(
    conn: sqlite3.Connection, tabela_chave: str, quando: date
) -> Decimal:
    """
    Taxa de juros de mora ACUMULADA (em %) da tabela de juros para o mês/ano
    de `quando`. Para o bloco de servidores, `tabela_chave` = "JUROS_SERVIDOR".

    Lembrando: a tabela acumula a taxa daquele mês ATÉ dez/2021 — então
    taxa(dez/2021) = 0 e meses anteriores têm taxas positivas e maiores quanto
    mais antiga a data.

    Levanta ValueError se não houver taxa para aquele (ano, mês).
    """
    row = conn.execute(
        "SELECT taxa FROM juros_fatores WHERE tabela_chave = ? AND ano = ? AND mes = ?",
        (tabela_chave, quando.year, quando.month),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"Juros '{tabela_chave}' não possui taxa para {quando:%Y-%m}."
        )
    return _d(row[0])


def fracao_juros_mora(
    conn: sqlite3.Connection,
    tabela_chave: str,
    data_inicio: date,
    data_fim: date,
) -> Decimal:
    """
    Fração de juros de mora acumulados entre `data_inicio` e `data_fim`,
    calculada como (taxa_acumulada(inicio) - taxa_acumulada(fim)) / 100.

    No R1 do bloco de servidores: data_inicio = data base do crédito,
    data_fim = dez/2021. Como taxa(dez/2021) = 0, o resultado é simplesmente
    taxa_acumulada(data_base) / 100 — mas a subtração mantém a fórmula geral
    e robusta caso a data fim mude.
    """
    t_inicio = taxa_juros_acumulada(conn, tabela_chave, data_inicio)
    t_fim = taxa_juros_acumulada(conn, tabela_chave, data_fim)
    return (t_inicio - t_fim) / CEM


# ------------------------------------------------------------------ #
# BACEN — Selic linear (R2)                                           #
# ------------------------------------------------------------------ #
def _primeiro_dia(quando: date) -> str:
    """Normaliza uma data para o 1º dia do mês em ISO (como o BACEN grava)."""
    return date(quando.year, quando.month, 1).isoformat()


def soma_selic_linear(
    conn: sqlite3.Connection,
    codigo_serie: int,
    data_inicio: date,
    data_fim: date,
) -> Decimal:
    """
    Soma LINEAR (simples) das taxas mensais da Selic entre `data_inicio` e
    `data_fim`, ambos INCLUSIVE (por mês/ano), devolvida como FRAÇÃO.

    No R2 do bloco federal: codigo_serie = 4390 (Selic mensal), data_inicio =
    dez/2021 (ou a data base, se posterior a nov/2021), data_fim = jul/2025.

    As observações BACEN estão em % a.m. (ex.: 0.77 = 0,77%); somamos os
    percentuais e dividimos por 100. O fator do regime é (1 + este retorno).
    """
    rows = conn.execute(
        """
        SELECT valor FROM observacoes
         WHERE codigo_serie = ? AND data >= ? AND data <= ?
         ORDER BY data
        """,
        (codigo_serie, _primeiro_dia(data_inicio), _primeiro_dia(data_fim)),
    ).fetchall()
    soma_pct = sum((_d(r[0]) for r in rows), Decimal("0"))
    return soma_pct / CEM


def soma_linear_serie(
    conn: sqlite3.Connection,
    codigo_serie: int,
    data_inicio: date,
    data_fim: date,
) -> Decimal:
    """
    Soma LINEAR (simples) das taxas mensais de QUALQUER série BACEN em % a.m.,
    entre `data_inicio` e `data_fim` inclusive, devolvida como fração.

    Mesma mecânica de `soma_selic_linear` — nome genérico para uso com outras
    séries, como a Taxa Legal (29543) no R3.
    """
    return soma_selic_linear(conn, codigo_serie, data_inicio, data_fim)


def meses_selic_disponiveis(
    conn: sqlite3.Connection,
    codigo_serie: int,
    data_inicio: date,
    data_fim: date,
) -> int:
    """
    Quantidade de meses da série encontrados na janela [inicio, fim] inclusive.
    Útil para conferir que não há buracos (compare com o nº de meses esperado).
    """
    row = conn.execute(
        """
        SELECT COUNT(*) FROM observacoes
         WHERE codigo_serie = ? AND data >= ? AND data <= ?
        """,
        (codigo_serie, _primeiro_dia(data_inicio), _primeiro_dia(data_fim)),
    ).fetchone()
    return int(row[0])


# ------------------------------------------------------------------ #
# BACEN — IPCA composto (R3) + utilitários de data                    #
# ------------------------------------------------------------------ #
def fim_do_mes(quando: date) -> date:
    """Último dia do mês de `quando`."""
    ultimo = calendar.monthrange(quando.year, quando.month)[1]
    return date(quando.year, quando.month, ultimo)


def dias_360(inicio: date, fim: date) -> int:
    """
    Dias entre duas datas pela convenção 360 (método US/NASD), equivalente à
    função DAYS360 do Excel com método padrão (FALSE).
    """
    d1, m1, y1 = inicio.day, inicio.month, inicio.year
    d2, m2, y2 = fim.day, fim.month, fim.year
    if d1 == 31:
        d1 = 30
    if d2 == 31 and d1 == 30:
        d2 = 30
    return (y2 - y1) * 360 + (m2 - m1) * 30 + (d2 - d1)


def ultimo_mes_serie(conn: sqlite3.Connection, codigo_serie: int) -> date:
    """Mês mais recente disponível de uma série BACEN (1º dia do mês)."""
    row = conn.execute(
        "SELECT MAX(data) FROM observacoes WHERE codigo_serie = ?",
        (codigo_serie,),
    ).fetchone()
    if not row or not row[0]:
        raise ValueError(f"Série BACEN {codigo_serie} não tem observações.")
    ano, mes, dia = (int(x) for x in row[0].split("-"))
    return date(ano, mes, 1)


def fator_ipca_composto(
    conn: sqlite3.Connection,
    codigo_serie: int,
    data_inicio: date,
    data_fim: date,
) -> Decimal:
    """
    Fator de correção IPCA COMPOSTO entre `data_inicio` e `data_fim`
    (inclusive, por mês/ano), como produtório de (1 + ipca_mensal/100).

    No R3 do bloco federal: codigo_serie = 433 (IPCA), data_inicio = ago/2025,
    data_fim = último IPCA divulgado.
    """
    rows = conn.execute(
        """
        SELECT valor FROM observacoes
         WHERE codigo_serie = ? AND data >= ? AND data <= ?
         ORDER BY data
        """,
        (codigo_serie, _primeiro_dia(data_inicio), _primeiro_dia(data_fim)),
    ).fetchall()
    fator = Decimal("1")
    for r in rows:
        fator *= (Decimal("1") + _d(r[0]) / CEM)
    return fator
