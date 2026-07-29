"""
Motor de cálculo do Valor de Face Bruto — Fase 5, fatia 3.

Segmentador genérico da linha do tempo do precatório. Dado o input
(PremissasAtivo) e uma data final (default: último IPCA disponível), ele:

1. Deriva as datas-chave: expedição (= corte do ano orçamentário) e fim da LOA.
2. Percorre os meses de (data_base+1) até a data final, classificando cada mês
   em um regime, com o R_graça SOBREPONDO o regime legal na sua janela.
3. Agrupa meses consecutivos de mesmo regime em segmentos e aplica o método de
   cada um, carregando os componentes (principal, juros, selic) por toda a cadeia.

Regimes (bloco federal de servidores):
  R_graça | janela [expedição+1 .. fim LOA] | IPCA-E (7478), SEM juros (global)
  R1      | até nov/2021                    | SICOM/IPCA-E (TCM_3), fecha em dez/2021
          |                                 | (divisor coef(12/2021)=1) + juros de mora
  R2      | dez/2021 .. jul/2025            | Selic linear (4390) sobre o global
  R3      | ago/2025 ..                     | IPCA (433) composto + 2% a.a. pro-rata
          |                                 | DAYS360 sobre o principal corrigido

Convenção de mês: as correções incidem a partir do mês da data base, INCLUSIVE
(o fator do primeiro regime já inclui o índice do mês da data base — é a lógica
do R1 desde o início, "coef(data_base) direto"). Segmentos são contíguos no grid
mensal (sem buracos nem sobreposição).

Tudo em Decimal.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import indices
from .modelo import PremissasAtivo, expedicao_do_ano_orcamento, fim_loa
from .perfis import (
    COMPENSATORIOS_CORTE,
    COMPENSATORIOS_FIM,
    COMPENSATORIOS_TAXA_ANTES,
    COMPENSATORIOS_TAXA_DEPOIS,
    CORTE_EC113,
    CORTE_EC136,
    JUROS_EC136_AA,
    obter_perfil,
)

# Séries BACEN comuns a todos os perfis federais (R2/R3/graça idênticos).
SERIE_IPCAE = 7478       # R_graça
SERIE_SELIC = 4390       # R2
SERIE_IPCA = 433         # R3 (correção)
SERIE_TAXA_LEGAL = 29543 # R3 (juros, expedição a partir de 2027)

# A partir desta LOA (ano orçamentário), o juros do R3 passa a ser a Taxa Legal
# (art. 406 CC / Lei 14.905/2024) em vez dos 2% a.a. pro-rata.
ANO_LOA_JUROS_TAXA_LEGAL = 2027
# As tabelas do R1 (correção SICOM + juros de mora) vêm do PERFIL:
#   perfil["r1_correcao"] e perfil["r1_juros"].

# Referência do SICOM: coef(dez/2021) = 1. O R1 sempre fecha aqui.
REF_SICOM = date(2021, 12, 1)

# Pivô do regime tributário: até 01/1996 correção (TCM_4, coef(01/1996)=1),
# a partir daí Selic linear só sobre o principal.
PIVO_TRIBUTARIO = date(1996, 1, 1)


# ------------------------------------------------------------------ #
# Utilitários de mês                                                  #
# ------------------------------------------------------------------ #
def _mes(d: date) -> date:
    return date(d.year, d.month, 1)


def _add_mes(d: date, n: int = 1) -> date:
    total = (d.year * 12 + (d.month - 1)) + n
    return date(total // 12, total % 12 + 1, 1)


def _ate_padrao(conn: sqlite3.Connection, codigo_serie: int) -> date:
    """
    Data final padrão do cálculo = mês corrente (hoje), limitado ao último mês
    já publicado da série. Assim o ativo é sempre atualizado "até a data atual",
    sem tentar corrigir meses cujo índice ainda não saiu.
    """
    hoje = _mes(date.today())
    ultimo = indices.ultimo_mes_serie(conn, codigo_serie)
    return min(hoje, ultimo)


# Marcos legais no grid mensal
_EC113_MES = _mes(CORTE_EC113)   # 2021-11
_EC136_MES = _mes(CORTE_EC136)     # 2025-07


# ------------------------------------------------------------------ #
# Resultado                                                           #
# ------------------------------------------------------------------ #
@dataclass
class ResultadoVF:
    principal: Decimal
    juros: Decimal
    selic: Decimal
    data_final: date
    trace: list = field(default_factory=list)

    @property
    def saldo(self) -> Decimal:
        return self.principal + self.juros + self.selic


# ------------------------------------------------------------------ #
# Classificação e segmentação                                        #
# ------------------------------------------------------------------ #
_COMP_CORTE_MES = _mes(COMPENSATORIOS_CORTE)   # 1997-06
_COMP_FIM_MES = _mes(COMPENSATORIOS_FIM)       # 2021-11


def _fracao_compensatorios(ini: date, fim: date, antes_adi: bool) -> Decimal:
    """
    Fração de juros COMPENSATÓRIOS simples acumulada nos meses [ini..fim].

    Capitalização simples: somamos a taxa de cada mês (sem compor).
      - antes_adi=True  : 1% a.m. em todos os meses (título anterior à ADI 2332)
      - antes_adi=False : 1% a.m. até 06/1997 e 0,5% a.m. de 07/1997 em diante
    Em ambos os casos, nada é acrescido a partir de dez/2021 (já embutido na
    Selic / Taxa Legal).

    Obs.: o corte legal é 10/06/1997 (meio de mês); no grid mensal tratamos
    junho/1997 inteiro à taxa de 1%.
    """
    total = Decimal("0")
    m = ini
    while m <= fim and m <= _COMP_FIM_MES:
        if antes_adi or m <= _COMP_CORTE_MES:
            total += COMPENSATORIOS_TAXA_ANTES
        else:
            total += COMPENSATORIOS_TAXA_DEPOIS
        m = _add_mes(m)
    return total


def _classificar(m: date, inicio_graca: date, loa_mes: date) -> str:
    """Regime de um mês. R_graça sobrepõe o regime legal na sua janela."""
    if inicio_graca <= m <= loa_mes:
        return "GRACA"
    if m <= _EC113_MES:
        return "R1"
    if m <= _EC136_MES:
        return "R2"
    return "R3"


def _segmentos(inicio: date, fim: date, inicio_graca: date, loa_mes: date):
    """Lista de (regime, primeiro_mes, ultimo_mes) cobrindo [inicio..fim]."""
    segs = []
    m = inicio
    while m <= fim:
        reg = _classificar(m, inicio_graca, loa_mes)
        ini = m
        while True:
            prox = _add_mes(m)
            if prox > fim or _classificar(prox, inicio_graca, loa_mes) != reg:
                break
            m = prox
        segs.append((reg, ini, m))
        m = _add_mes(m)
    return segs


# ------------------------------------------------------------------ #
# Motor                                                               #
# ------------------------------------------------------------------ #
def calcular_vf_bruto(
    conn: sqlite3.Connection,
    premissas: PremissasAtivo,
    ate: date | None = None,
) -> ResultadoVF:
    """
    Calcula o Valor de Face Bruto do ativo até `ate` (default: último IPCA).
    Retorna componentes (principal, juros, selic) e um trace por segmento.
    """
    premissas.validar()

    perfil = obter_perfil(premissas.perfil)

    # Perfis com regime próprio usam uma função dedicada (escape do segmentador).
    if perfil.get("motor") == "tributario":
        return _calcular_tributario(conn, premissas, perfil, ate)

    tabela_sicom = perfil["r1_correcao"]   # tabela CJF de correção do R1
    tabela_juros = perfil["r1_juros"]      # tabela de juros de mora do R1

    expedicao = expedicao_do_ano_orcamento(premissas.ano_orcamento)
    loa_mes = _mes(fim_loa(premissas.ano_orcamento))
    inicio_graca = _add_mes(_mes(expedicao))

    if ate is None:
        ate = _ate_padrao(conn, SERIE_IPCA)   # hoje, limitado ao último IPCA
    fim_mes = _mes(ate)

    principal = premissas.valor_principal
    juros = premissas.valor_juros
    selic = Decimal("0")

    inicio = _mes(premissas.data_base)  # correções a partir do mês da data base (inclusive)
    res = ResultadoVF(principal, juros, selic, ate)

    for regime, ini, fim in _segmentos(inicio, fim_mes, inicio_graca, loa_mes):
        if regime == "GRACA":
            f = indices.fator_ipca_composto(conn, SERIE_IPCAE, ini, fim)
            principal *= f; juros *= f; selic *= f
            detalhe = {"fator": f}

        elif regime == "R1":
            # SICOM fecha sempre na referência dez/2021 (coef(12/2021)=1).
            # Para um segmento que termina antes (ex.: véspera da graça), o
            # divisor é coef(mês seguinte ao fim do segmento).
            coef_ini = indices.fator_correcao_sicom(conn, tabela_sicom, ini)
            coef_fim = indices.fator_correcao_sicom(conn, tabela_sicom, _add_mes(fim))
            f = coef_ini / coef_fim
            fr_mora = indices.fracao_juros_mora(conn, tabela_juros, ini, _add_mes(fim))
            principal *= f; juros *= f; selic *= f
            jmora = principal * fr_mora
            juros += jmora
            detalhe = {"fator": f, "juros_mora_frac": fr_mora, "juros_mora": jmora}

            # Juros COMPENSATÓRIOS (desapropriação sem precatório expedido):
            # simples, sobre o principal corrigido, dentro do R1.
            if perfil.get("juros_compensatorios") and not premissas.precatorio_expedido:
                fr_comp = _fracao_compensatorios(
                    ini, fim, premissas.transito_antes_adi2332
                )
                jcomp = principal * fr_comp
                juros += jcomp
                detalhe["compensatorios_frac"] = fr_comp
                detalhe["juros_compensatorios"] = jcomp

        elif regime == "R2":
            soma = indices.soma_selic_linear(conn, SERIE_SELIC, ini, fim)
            base = principal + juros + selic
            selic += base * soma
            detalhe = {"soma_selic": soma, "selic_acrescida": base * soma}

        else:  # R3
            # Correção: IPCA composto sobre todos os componentes.
            f = indices.fator_ipca_composto(conn, SERIE_IPCA, ini, fim)
            principal *= f; juros *= f; selic *= f

            # Juros do R3: depende da LOA (ano orçamentário) do precatório.
            #   LOA >= 2027 -> Taxa Legal (série 29543), soma linear
            #   LOA <  2027 -> 2% a.a. pro-rata (DAYS360 desde ago/2025)
            # Em ambos os casos, incide sobre o principal já corrigido.
            if premissas.ano_orcamento >= ANO_LOA_JUROS_TAXA_LEGAL:
                fr_j = indices.soma_linear_serie(conn, SERIE_TAXA_LEGAL, ini, fim)
                j_r3 = principal * fr_j
                detalhe = {
                    "fator": f, "juros_modo": "taxa_legal",
                    "juros_frac": fr_j, "juros_r3": j_r3,
                }
            else:
                dias = indices.dias_360(_add_mes(_EC136_MES), indices.fim_do_mes(fim))
                fr_j = JUROS_EC136_AA * Decimal(dias) / Decimal("360")
                j_r3 = principal * fr_j
                detalhe = {
                    "fator": f, "juros_modo": "2pct_prorata",
                    "dias_360": dias, "juros_frac": fr_j, "juros_r3": j_r3,
                }
            juros += j_r3

        res.trace.append({
            "regime": regime,
            "de": f"{ini:%Y-%m}", "ate": f"{fim:%Y-%m}",
            "saldo": principal + juros + selic,
            **detalhe,
        })

    res.principal, res.juros, res.selic = principal, juros, selic
    return res


# ------------------------------------------------------------------ #
# Motor dedicado: Indébito Tributário (regime próprio)                #
# ------------------------------------------------------------------ #
def _calcular_tributario(
    conn: sqlite3.Connection,
    premissas: PremissasAtivo,
    perfil: dict,
    ate: date | None = None,
) -> ResultadoVF:
    """
    Regime do indébito tributário (sem graça, sem cortes EC113/EC136):

    1. Se data base < 01/1996: correção composta (TCM_4, ref. 01/1996) + juros de
       mora (JUROS_TRIBUTARIO), sobre principal e juros -> valor em 01/1996.
    2. De max(01/1996, data base) até `ate` (default: último Selic): Selic LINEAR
       (BACEN 4390) SOMENTE sobre o principal; o componente de juros fica fixo.

    O componente `selic` do resultado guarda o acréscimo da Selic sobre o
    principal (transparência); `principal` fica na sua base (corrigida a 1996).
    """
    tabela_correcao = perfil["r1_correcao"]  # TCM_4
    tabela_juros = perfil["r1_juros"]        # JUROS_TRIBUTARIO

    if ate is None:
        ate = _ate_padrao(conn, SERIE_SELIC)   # hoje, limitado ao último Selic
    fim = _mes(ate)

    principal = premissas.valor_principal
    juros = premissas.valor_juros
    data_base = _mes(premissas.data_base)
    trace = []

    # --- Segmento 1: correção até 01/1996 (só se a data base for anterior) ---
    if data_base < PIVO_TRIBUTARIO:
        coef_ini = indices.fator_correcao_sicom(conn, tabela_correcao, data_base)
        coef_piv = indices.fator_correcao_sicom(conn, tabela_correcao, PIVO_TRIBUTARIO)
        f = coef_ini / coef_piv
        fr_mora = indices.fracao_juros_mora(conn, tabela_juros, data_base, PIVO_TRIBUTARIO)
        principal *= f
        juros *= f
        jmora = principal * fr_mora
        juros += jmora
        trace.append({
            "regime": "CORRECAO<1996", "de": f"{data_base:%Y-%m}", "ate": "1996-01",
            "fator": f, "juros_mora": jmora, "saldo": principal + juros,
        })
        selic_inicio = PIVO_TRIBUTARIO
    else:
        selic_inicio = data_base

    # --- Segmento 2: Selic linear só sobre o principal ---
    soma = indices.soma_selic_linear(conn, SERIE_SELIC, selic_inicio, fim)
    selic_acrescida = principal * soma
    trace.append({
        "regime": "SELIC", "de": f"{selic_inicio:%Y-%m}", "ate": f"{fim:%Y-%m}",
        "soma_selic": soma, "selic": selic_acrescida,
        "saldo": principal + selic_acrescida + juros,
    })

    return ResultadoVF(principal, juros, selic_acrescida, ate, trace)
