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
          |   pré-expedição                 |   IPCA-E/IPCA-15 (7478) + Taxa Legal (29543)
          |   pós-graça                     |   IPCA (433) + juros de 2% a.a. pro-rata

O R3 não é um regime único: a EXPEDIÇÃO do precatório muda a sua composição.
Antes da expedição o crédito ainda é dívida judicial em curso e se corrige pelo
IPCA-E (= IPCA-15, o índice forense) com os juros da Taxa Legal. Depois do fim
do período de graça o crédito é precatório vencido e não pago, e aí valem o IPCA
do IBGE e os juros de mora de 2% a.a. da EC 136. Como a janela de graça fica
entre os dois, cada segmento de R3 cai inteiramente de um lado ou do outro, e
os juros de cada um correm apenas dentro do próprio segmento — o período de
graça não gera juros e não entra na contagem de nenhum dos dois.

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
SERIE_TAXA_LEGAL = 29543 # juros do R3 pré-expedição

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


def _ultimo_mes_fechado(conn: sqlite3.Connection, codigo_serie: int) -> date:
    """
    Último mês da série que já está FECHADO — isto é, publicado e anterior ao
    mês corrente.

    O corte pelo mês corrente não é zelo excessivo: há séries que publicam o
    mês em curso ainda incompleto. A Taxa Legal (29543) é o caso claro — em
    01/10/2026 ela já trazia outubro com 0,37902%, contra 1,499119% de
    setembro inteiro. Somar esse valor seria contar um mês pela metade.
    """
    hoje = _mes(date.today())
    return min(indices.ultimo_mes_serie(conn, codigo_serie), _add_mes(hoje, -1))


def _ate_padrao(conn: sqlite3.Connection, *codigos_serie: int) -> date:
    """
    Data final padrão do cálculo: o último mês fechado em TODAS as séries
    informadas. Assim o ativo é sempre atualizado até onde os dados permitem,
    sem tentar corrigir meses cujo índice ainda não saiu nem meses incompletos.

    Recebe mais de uma série porque um regime pode depender de duas: o R3
    pré-expedição, por exemplo, usa o IPCA-15 na correção e a Taxa Legal nos
    juros, e só pode avançar até onde as duas tiverem fechado.
    """
    return min(_ultimo_mes_fechado(conn, c) for c in codigos_serie)


def _series_do_fim(pre_expedicao: bool) -> tuple[int, ...]:
    """
    Séries que limitam a data final, conforme o regime que fecha o cálculo.

    Usar sempre o IPCA (433) seria errado: ele é a série do R3 PÓS-GRAÇA. Um
    crédito que ainda não teve o precatório expedido fecha em R3
    PRÉ-EXPEDIÇÃO, que corrige pelo IPCA-15 e aplica a Taxa Legal — e essas
    duas costumam estar publicadas à frente do IPCA, de modo que ancorar no
    433 interrompia o cálculo antes do que os dados permitiam.
    """
    if pre_expedicao:
        return (SERIE_IPCA15, SERIE_TAXA_LEGAL)
    return (SERIE_IPCA,)


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

    # Depósito judicial puro: só poupança, sem regimes judiciais nem LOA.
    if perfil.get("motor") == "deposito_judicial":
        return _calcular_deposito_judicial(conn, premissas, ate)

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
        # A data final depende de QUAL regime fecha o cálculo, porque cada um
        # lê séries diferentes. Se mesmo esticando até o limite das séries do
        # R3 pré-expedição o cálculo ainda termina antes da graça, então é esse
        # o regime final e são essas as séries que mandam; caso contrário o
        # fecho é pós-graça (ou dentro da graça) e quem manda é o IPCA.
        candidato = _ate_padrao(conn, *_series_do_fim(pre_expedicao=True))
        pre = candidato < inicio_graca
        ate = candidato if pre else _ate_padrao(conn, *_series_do_fim(False))
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

            # Juros COMPENSATÓRIOS (desapropriação): simples, sobre o principal
            # corrigido, dentro do R1. São OPT-IN — ver
            # PremissasAtivo.aplicar_juros_compensatorios. Quando o principal
            # informado vem de um ofício requisitório, os compensatórios já
            # estão embutidos nele e não devem incidir de novo.
            if perfil.get("juros_compensatorios"):
                if premissas.aplicar_juros_compensatorios:
                    fr_comp = _fracao_compensatorios(
                        ini, fim, premissas.transito_antes_adi2332
                    )
                    jcomp = principal * fr_comp
                    juros += jcomp
                    detalhe["compensatorios"] = "aplicados"
                    detalhe["compensatorios_frac"] = fr_comp
                    detalhe["juros_compensatorios"] = jcomp
                else:
                    # Fica no trace para o laudo mostrar que a omissão foi
                    # deliberada, e não um esquecimento.
                    detalhe["compensatorios"] = "nao aplicados (opt-in desligado)"

        elif regime == "R2":
            soma = indices.soma_selic_linear(conn, SERIE_SELIC, ini, fim)
            base = principal + juros + selic
            selic += base * soma
            detalhe = {"soma_selic": soma, "selic_acrescida": base * soma}

        else:  # R3
            # A composição do R3 é definida pela POSIÇÃO do segmento em relação
            # à expedição do precatório — não pelo ano da LOA:
            #
            #   PRÉ-EXPEDIÇÃO — o crédito ainda é dívida judicial em curso:
            #       correção pelo IPCA-E (= IPCA-15) + juros da Taxa Legal.
            #
            #   PÓS-GRAÇA — precatório expedido, vencido e não pago:
            #       correção pelo IPCA (IBGE) + juros de mora de 2% a.a.
            #
            # A janela de graça separa os dois casos no grid mensal, então um
            # segmento de R3 fica inteiramente de um lado ou do outro. Nos dois
            # casos os juros correm SOMENTE dentro do próprio segmento: é isso
            # que impede o período de graça (que não gera juros) de ser contado,
            # e impede que os dois R3 se sobreponham quando ambos existem.
            pre_expedicao = fim < inicio_graca

            if pre_expedicao:
                f = indices.fator_ipca_composto(conn, SERIE_IPCA15, ini, fim)
                principal *= f; juros *= f; selic *= f
                fr_j = indices.soma_linear_serie(conn, SERIE_TAXA_LEGAL, ini, fim)
                detalhe = {
                    "fator": f, "correcao": "ipca15",
                    "juros_modo": "taxa_legal", "posicao": "pre_expedicao",
                    "juros_frac": fr_j,
                }
            else:
                f = indices.fator_ipca_composto(conn, SERIE_IPCA, ini, fim)
                principal *= f; juros *= f; selic *= f
                # 2% a.a. pro-rata em dias corridos base 360, do início DESTE
                # segmento até o fim do mês final. No caso simples (sem R3
                # pré-expedição) o segmento já começa em ago/2025, o primeiro
                # mês do regime da EC 136.
                dias = indices.dias_360(ini, indices.fim_do_mes(fim))
                fr_j = JUROS_EC136_AA * Decimal(dias) / Decimal("360")
                detalhe = {
                    "fator": f, "correcao": "ipca",
                    "juros_modo": "2pct_prorata", "posicao": "pos_graca",
                    "dias_360": dias, "juros_frac": fr_j,
                }
            # Os juros do R3 incidem APENAS sobre o principal corrigido.
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
# Depósito judicial: apenas poupança sobre o montante depositado       #
# ------------------------------------------------------------------ #
def _calcular_deposito_judicial(
    conn: sqlite3.Connection,
    premissas: PremissasAtivo,
    ate: date | None = None,
) -> ResultadoVF:
    """
    Perfil `deposito_judicial`: o montante depositado rende poupança da data
    do depósito até a data de cálculo. Nada mais incide — não há correção por
    regime judicial, expedição, período de graça nem LOA.

    O montante entra como principal; juros e selic ficam zerados, já que não
    há decomposição a preservar num depósito.
    """
    from . import poupanca as _poupanca

    deposito = premissas.data_deposito
    data_calculo = ate or date.today()

    res_poup = _poupanca.acumular(
        conn, premissas.valor_principal, deposito, data_calculo
    )

    trace = [{
        "regime": "POUPANCA",
        "de": f"{deposito:%Y-%m}",
        "ate": f"{data_calculo:%Y-%m}",
        "data_deposito": deposito.isoformat(),
        "montante_depositado": premissas.valor_principal,
        "aniversarios": res_poup.aniversarios,
        "fator": res_poup.fator,
        "saldo": res_poup.valor_final,
        "detalhe": res_poup.trace,
    }]

    return ResultadoVF(
        res_poup.valor_final, Decimal("0"), Decimal("0"), data_calculo, trace
    )


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
