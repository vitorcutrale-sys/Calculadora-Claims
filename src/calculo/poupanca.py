"""
Accrual de poupança para precatórios JÁ DEPOSITADOS — Fase 5.

Depois de depositado, o precatório deixa de ser corrigido pelos regimes
judiciais: o valor fica em conta poupança e rende por **aniversário mensal**.
Nos precatórios federais o depósito é feito pelo valor BRUTO — impostos,
honorários e share incidem depois, sobre o valor já acrescido de poupança.

Mecânica (série BACEN 195, tabela `poupanca_periodos`):

1. A remuneração começa no dia do depósito.
2. Cada registro da série é um período de aniversário: `data_inicio`,
   `data_fim` e a taxa do mês. O encadeamento se faz procurando o período
   cujo início é o `data_fim` do anterior.
3. Períodos completos (que terminam até a data de cálculo) são capitalizados
   integralmente, um após o outro.
4. Do último aniversário até a data de cálculo aplica-se pro-rata em DIAS
   ÚTEIS base 252, na convenção exponencial do mercado brasileiro:

       fator = (1 + taxa_mensal) ** (DU / 21)      # 21 = 252/12

   Num mês cheio (~21 DU) isso reproduz exatamente a taxa mensal. Os dias
   úteis excluem sábados, domingos e os feriados bancários da ANBIMA
   (tabela `feriados`), contados no intervalo [último aniversário, data de
   cálculo).

Regra dos dias 29, 30 e 31: a série só tem dias de aniversário de 1 a 28.
Depósitos feitos nos dias 29 a 31 têm aniversário no dia 1º do mês seguinte
— e o trecho entre o depósito e esse dia 1º não é remunerado, como na
prática bancária.

Tudo em Decimal.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .. import db as _db

CEM = Decimal("100")
# Base 252 expressa por mês: 252 / 12.
DIAS_UTEIS_MES = Decimal("21")


@dataclass
class ResultadoPoupanca:
    """Valor após o accrual da poupança, com a trilha de cada período."""
    valor_inicial: Decimal
    valor_final: Decimal
    data_deposito: date
    data_calculo: date
    aniversarios: int
    trace: list = field(default_factory=list)

    @property
    def fator(self) -> Decimal:
        if self.valor_inicial == 0:
            return Decimal("1")
        return self.valor_final / self.valor_inicial


# ------------------------------------------------------------------ #
# Dias úteis                                                          #
# ------------------------------------------------------------------ #
def contar_dias_uteis(conn: sqlite3.Connection, de: date, ate: date) -> int:
    """
    Dias úteis no intervalo [de, ate) — inclui o início, exclui o fim.

    Exclui sábados, domingos e feriados bancários (ANBIMA).
    """
    if ate <= de:
        return 0
    feriados = _db.listar_feriados(conn, de.isoformat(), ate.isoformat())
    total = 0
    d = de
    while d < ate:
        if d.weekday() < 5 and d.isoformat() not in feriados:
            total += 1
        d += timedelta(days=1)
    return total


# ------------------------------------------------------------------ #
# Aniversário                                                         #
# ------------------------------------------------------------------ #
def aniversario_efetivo(d: date) -> date:
    """
    Data de aniversário aplicável a um depósito feito em `d`.

    Dias 1 a 28 são o próprio dia; dias 29, 30 e 31 têm aniversário no
    dia 1º do mês seguinte (a série 195 só publica os dias 1 a 28).
    """
    if d.day <= 28:
        return d
    ano, mes = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(ano, mes, 1)


# ------------------------------------------------------------------ #
# Accrual                                                             #
# ------------------------------------------------------------------ #
def acumular(
    conn: sqlite3.Connection,
    valor: Decimal,
    data_deposito: date,
    data_calculo: date,
) -> ResultadoPoupanca:
    """
    Rende `valor` em poupança do depósito até a data de cálculo.

    Capitaliza os aniversários completos e aplica pro-rata em dias úteis
    (base 252) no período em aberto.
    """
    res = ResultadoPoupanca(valor, valor, data_deposito, data_calculo, 0)

    if data_calculo <= data_deposito:
        return res

    cursor = aniversario_efetivo(data_deposito)
    if cursor != data_deposito:
        res.trace.append({
            "etapa": "stub inicial sem remuneração",
            "de": data_deposito.isoformat(),
            "ate": cursor.isoformat(),
            "observacao": "depósito nos dias 29-31: aniversário no dia 1º seguinte",
        })

    atual = valor
    while True:
        periodo = _db.periodo_poupanca(conn, cursor.isoformat())
        if periodo is None:
            raise ValueError(
                f"Poupança: não há período de aniversário iniciando em "
                f"{cursor:%Y-%m-%d} na tabela poupanca_periodos. A série 195 "
                f"cobre de 04/05/2012 em diante — rode "
                f"'python coletar_indices.py --so-poupanca' se o histórico "
                f"estiver desatualizado."
            )

        taxa = Decimal(str(periodo["taxa"])) / CEM
        fim = date.fromisoformat(periodo["data_fim"])

        if fim <= data_calculo:
            # Aniversário completo: capitaliza a taxa integral.
            atual *= (Decimal("1") + taxa)
            res.aniversarios += 1
            res.trace.append({
                "etapa": "aniversário",
                "de": cursor.isoformat(),
                "ate": fim.isoformat(),
                "taxa": taxa,
                "saldo": atual,
            })
            cursor = fim
            if cursor == data_calculo:
                break
            continue

        # Período em aberto: pro-rata em dias úteis, base 252.
        du = contar_dias_uteis(conn, cursor, data_calculo)
        if du > 0:
            expoente = Decimal(du) / DIAS_UTEIS_MES
            fator = (Decimal("1") + taxa) ** expoente
            atual *= fator
        else:
            fator = Decimal("1")
        res.trace.append({
            "etapa": "pro-rata (base 252)",
            "de": cursor.isoformat(),
            "ate": data_calculo.isoformat(),
            "taxa": taxa,
            "dias_uteis": du,
            "fator": fator,
            "saldo": atual,
        })
        break

    res.valor_final = atual
    return res
