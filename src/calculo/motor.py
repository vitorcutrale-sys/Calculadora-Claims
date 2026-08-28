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
  R3      | ago/2025 ..                     | depende da POSIÇÃO na linha do tempo:
          |   pré-expedição                 |   IPCA-15 (7478) + Taxa Legal (29543)
          |   pós-graça                     |   IPCA (433) + 2% a.a. pro-rata
          |                                 |   (ou Taxa Legal se LOA >= 2027)

Convenção de mês: as correções incidem a partir do mês da data base, INCLUSIVE
(o fator do primeiro regime já inclui o índice do mês da data base — é a lógica
do R1 desde o início, "coef(data_base) direto"). Segmentos são contíguos no grid
mensal (sem buracos nem sobreposição).

Tudo em Decimal.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
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
SERIE_IPCA15 = 7478      # R_graça e R3 pré-expedição (IPCA-15, a "IPCA-E" forense)
SERIE_SELIC = 4390       # R2
SERIE_IPCA = 433         # R3 pós-graça (correção)
SERIE_TAXA_LEGAL = 29543 # juros: R3 pré-expedição sempre; pós-graça se LOA >= 2027

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

    # Precatório já DEPOSITADO: a correção judicial para na data do depósito e
    # o valor passa a render poupança por aniversário até a data de cálculo.
    if premissas.precatorio_depositado and premissas.data_deposito:
        return _aplicar_deposito(conn, premissas, perfil, ate)

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
            f = indices.fator_ipca_composto(conn, SERIE_IPCA15, ini, fim)
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
            # A composição do R3 depende de ONDE ele cai na linha do tempo:
            #
            #   PRÉ-EXPEDIÇÃO (correção entre a data base e a expedição, no
            #   trecho posterior a 01/08/2025) -> IPCA-15 + Taxa Legal.
            #
            #   PÓS-GRAÇA (precatório expedido e não pago) -> IPCA + juros de
            #   2% a.a. pro-rata, ou Taxa Legal se a LOA for >= 2027.
            #
            # Como a janela de graça separa os dois casos no grid mensal, um
            # segmento de R3 fica inteiramente de um lado ou do outro.
            pre_expedicao = fim < inicio_graca

            if pre_expedicao:
                f = indices.fator_ipca_composto(conn, SERIE_IPCA15, ini, fim)
                principal *= f; juros *= f; selic *= f
                fr_j = indices.soma_linear_serie(conn, SERIE_TAXA_LEGAL, ini, fim)
                j_r3 = principal * fr_j
                detalhe = {
                    "fator": f, "correcao": "ipca15",
                    "juros_modo": "taxa_legal", "posicao": "pre_expedicao",
                    "juros_frac": fr_j, "juros_r3": j_r3,
                }
            else:
                f = indices.fator_ipca_composto(conn, SERIE_IPCA, ini, fim)
                principal *= f; juros *= f; selic *= f
                if premissas.ano_orcamento >= ANO_LOA_JUROS_TAXA_LEGAL:
                    fr_j = indices.soma_linear_serie(conn, SERIE_TAXA_LEGAL, ini, fim)
                    detalhe = {
                        "fator": f, "correcao": "ipca",
                        "juros_modo": "taxa_legal", "posicao": "pos_graca",
                        "juros_frac": fr_j,
                    }
                else:
                    dias = indices.dias_360(
                        _add_mes(_EC136_MES), indices.fim_do_mes(fim)
                    )
                    fr_j = JUROS_EC136_AA * Decimal(dias) / Decimal("360")
                    detalhe = {
                        "fator": f, "correcao": "ipca",
                        "juros_modo": "2pct_prorata", "posicao": "pos_graca",
                        "dias_360": dias, "juros_frac": fr_j,
                    }
                j_r3 = principal * fr_j
                detalhe["juros_r3"] = j_r3
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
# Precatório depositado: corrige até o depósito e rende poupança       #
# ------------------------------------------------------------------ #
def _aplicar_deposito(
    conn: sqlite3.Connection,
    premissas: PremissasAtivo,
    perfil: dict,
    ate: date | None = None,
) -> ResultadoVF:
    """
    Fluxo do precatório já depositado:

    1. Corrige pelos regimes judiciais SOMENTE até a data do depósito.
    2. Do depósito até a data de cálculo, rende poupança por aniversário
       mensal (com pro-rata em dias úteis base 252 no período em aberto).

    Nos federais o depósito é feito pelo valor BRUTO, então a poupança
    incide sobre o saldo bruto. O fator é aplicado proporcionalmente aos
    três componentes, preservando a composição principal/juros/selic —
    que segue importando para PSS e IR-RRA na camada de valor líquido.
    """
    from . import poupanca as _poupanca

    deposito = premissas.data_deposito
    data_calculo = ate or date.today()

    # Etapa 1 — correção judicial até o depósito.
    premissas_ate_deposito = replace(
        premissas, precatorio_depositado=False, data_deposito=None
    )
    if perfil.get("motor") == "tributario":
        base = _calcular_tributario(conn, premissas_ate_deposito, perfil, deposito)
    else:
        base = calcular_vf_bruto(conn, premissas_ate_deposito, ate=deposito)

    # Etapa 2 — poupança do depósito até a data de cálculo.
    res_poup = _poupanca.acumular(conn, base.saldo, deposito, data_calculo)
    fator = res_poup.fator

    principal = base.principal * fator
    juros = base.juros * fator
    selic = base.selic * fator

    trace = list(base.trace)
    trace.append({
        "regime": "POUPANCA",
        "de": f"{deposito:%Y-%m}",
        "ate": f"{data_calculo:%Y-%m}",
        "data_deposito": deposito.isoformat(),
        "aniversarios": res_poup.aniversarios,
        "fator": fator,
        "saldo": principal + juros + selic,
        "detalhe": res_poup.trace,
    })

    return ResultadoVF(principal, juros, selic, data_calculo, trace)


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
