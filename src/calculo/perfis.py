"""
Perfis de cálculo por tipo de ativo — receitas declarativas (Fase 5).

Cada perfil descreve, de forma declarativa, COMO o saldo de um tipo de ativo
evolui no tempo: a sequência de regimes legais encadeados (R1 -> R2 -> R3),
suas datas de corte e qual índice/juros cada um aplica. O motor (motor.py,
próxima fatia) é genérico e apenas "executa a receita".

Adicionar um novo tipo de ativo = adicionar um perfil aqui (sem mexer no motor).
Casos com lógica realmente peculiar podem apontar para uma função dedicada.

--- Bloco 1: Precatório Federal de Servidores ---
Os três regimes encadeados, confirmados com o usuário:

  R1 (pré-EC 113)  | até 30/11/2021 | correção composta pela tabela SICOM/CJF
                   |                | de servidores (TCM seq. 3), sobre principal+juros
  R2 (EC 113)      | 01/12/2021 a   | Selic LINEAR (soma simples das Selic mensais)
                   | 31/07/2025     | sobre o saldo que veio do R1
  R3 (EC 136)       | a partir de    | correção IPCA (composta) + juros de 2% a.a.
                   | 01/08/2025     | aplicados como 2%/12 ao mês sobre o principal corrigido

A curva vai da data base até a expedição (ver modelo.expedicao_do_ano_orcamento).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

# Cortes de regime (marcos legais), comuns ao bloco federal.
CORTE_EC113 = date(2021, 11, 30)   # fim do R1 / início do R2 (EC 113)
CORTE_EC136 = date(2025, 7, 31)     # fim do R2 / início do R3 (EC 136)

# Juros do regime R3: 2% ao ano, aplicados mensalmente como 2%/12.
JUROS_EC136_AA = Decimal("0.02")

# ------------------------------------------------------------------ #
# Tributação na camada de valor líquido                               #
# ------------------------------------------------------------------ #
# Duas camadas mutuamente exclusivas, definidas pelo tipo de verba:
#
#   perfil com `verba_remuneratoria: True`  -> PSS + IR-RRA
#       (verba ligada à remuneração de servidor; PSS sobre o principal e
#        IR-RRA pela tabela progressiva mensal)
#
#   demais perfis                            -> IRRF de 3% sobre o global
#       (art. 27 da Lei 10.833/2003: nos pagamentos de precatório ou RPV da
#        Justiça Federal, a instituição financeira retém 3% sobre o montante
#        pago, SEM quaisquer deduções)
IRRF_PRECATORIO_FEDERAL = Decimal("0.03")

# ------------------------------------------------------------------ #
# Juros COMPENSATÓRIOS — desapropriação                               #
# ------------------------------------------------------------------ #
# Aplicáveis quando o precatório ainda NÃO foi expedido. Incidem sobre o
# principal corrigido, dentro do R1, em capitalização SIMPLES.
#
# Regime do Manual de Cálculos da Justiça Federal (título transitado APÓS a
# ADI 2332):
#   até 10/06/1997          -> 1%   a.m. simples (Súmula 618/STF, 110/TFR)
#   11/06/1997 a nov/2021   -> 0,5% a.m. simples (art. 15-A do DL 3.365/1941,
#                              introduzido pela MP 1.577/1997; ADI 2332)
#   dez/2021 a ago/2025     -> já embutidos na Selic (art. 3º da EC 113/2021)
#   a partir de set/2025    -> já embutidos na Taxa Legal (Tema 1.419/STF,
#                              art. 406 CC, Lei 14.905/2024)
#
# Se o título transitou ANTES da ADI 2332, aplica-se 1% a.m. simples em todo
# o período (a redução para 0,5% não o alcança).
COMPENSATORIOS_CORTE = date(1997, 6, 10)        # 1% até aqui; 0,5% a partir de 11/06/1997
COMPENSATORIOS_FIM = date(2021, 11, 30)         # depois disso, sem taxa adicional
COMPENSATORIOS_TAXA_ANTES = Decimal("0.01")     # 1% a.m. simples
COMPENSATORIOS_TAXA_DEPOIS = Decimal("0.005")   # 0,5% a.m. simples


PERFIS = {
    "federal_servidores": {
        "nome": "Precatório Federal — Servidores e Empregados Públicos",
        "descricao": (
            "Ações condenatórias de servidores e empregados públicos na Justiça "
            "Federal. Correção encadeada SICOM servidores -> Selic linear -> IPCA+juros."
        ),
        # Tabelas do R1 lidas pelo motor (correção monetária + juros de mora).
        "r1_correcao": "TCM_3",          # SICOM servidores (IPCA-E), referência dez/2021
        "r1_juros": "JUROS_SERVIDOR",    # Tabela de Juros de Mora - servidores
        # Verba de natureza remuneratória -> sujeita aos descontos de PSS e
        # IR-RRA na camada de valor líquido (ver src/calculo/liquido.py).
        "verba_remuneratoria": True,
        # Os três regimes, em ordem cronológica. O motor encadeia o saldo.
        "regimes": [
            {
                "id": "R1",
                "rotulo": "Pré-EC 113 — SICOM servidores (composto)",
                "ate": CORTE_EC113,
                "metodo": "sicom_composto",
                "fonte": ("CJF", "TCM_3"),     # tabela de correção monetária, servidores
                "incide_sobre": "principal_e_juros",
            },
            {
                "id": "R2",
                "rotulo": "EC 113 — Selic linear",
                "de": CORTE_EC113,
                "ate": CORTE_EC136,
                "metodo": "selic_linear",
                "fonte": ("BACEN", 4390),       # Selic mensal
                "incide_sobre": "principal_e_juros",
            },
            {
                "id": "R3",
                "rotulo": "EC 136 — IPCA composto + juros 2% a.a.",
                "de": CORTE_EC136,
                "metodo": "ipca_composto_mais_juros",
                "fonte": ("BACEN", 433),        # IPCA
                "juros_aa": JUROS_EC136_AA,      # 2% a.a. -> 2%/12 ao mês
                "incide_sobre": "principal",
            },
        ],
    },
    "federal_fazenda_geral": {
        "nome": "Precatório Federal — Ações Condenatórias (Devedor Fazenda Pública)",
        "descricao": (
            "Mesma lógica encadeada do perfil de servidores, porém o R1 usa a "
            "tabela TCM_5 (SICOM, Tabela de Correção Monetária da categoria 5 - "
            "Fazenda Pública) para a correção e a Tabela de Juros de Mora Geral "
            "(não-tributário) para os juros. R2 (Selic linear), R3 (IPCA + 2%) e "
            "R_graça (IPCA-E) são idênticos. Obs.: VHI_5 não serve para correção "
            "(série incompleta, sem dez/2021); usa-se a TCM_5."
        ),
        "r1_correcao": "TCM_5",              # SICOM categoria 5 (Fazenda Pública)
        "r1_juros": "JUROS_NAO_TRIBUTARIO",  # Tabela de Juros de Mora Geral (não-tributário)
        "regimes": [
            {
                "id": "R1",
                "rotulo": "Pré-EC 113 — SICOM Fazenda Pública (TCM_5)",
                "ate": CORTE_EC113,
                "metodo": "sicom_composto",
                "fonte": ("CJF", "TCM_5"),
                "incide_sobre": "principal_e_juros",
            },
            {
                "id": "R2",
                "rotulo": "EC 113 — Selic linear",
                "de": CORTE_EC113,
                "ate": CORTE_EC136,
                "metodo": "selic_linear",
                "fonte": ("BACEN", 4390),
                "incide_sobre": "principal_e_juros",
            },
            {
                "id": "R3",
                "rotulo": "EC 136 — IPCA composto + juros 2% a.a.",
                "de": CORTE_EC136,
                "metodo": "ipca_composto_mais_juros",
                "fonte": ("BACEN", 433),
                "juros_aa": JUROS_EC136_AA,
                "incide_sobre": "principal",
            },
        ],
    },
    "federal_naofazenda_geral": {
        "nome": "Precatório Federal — Ações Condenatórias (Devedor não Fazenda Pública)",
        "descricao": (
            "Categoria 6 (devedor não enquadrado como Fazenda Pública). R1 usa a "
            "TCM_6 para a correção e a Tabela de Juros de Mora Geral (não-tributário). "
            "Nos dados atuais a TCM_6 é proporcional à TCM_5, então o efeito prático "
            "é igual ao federal_fazenda_geral — perfil separado para receber ajustes "
            "futuros próprios da categoria."
        ),
        "r1_correcao": "TCM_6",              # SICOM categoria 6 (devedor não-Fazenda)
        "r1_juros": "JUROS_NAO_TRIBUTARIO",  # Tabela de Juros de Mora Geral (não-tributário)
        "regimes": [
            {
                "id": "R1",
                "rotulo": "Pré-EC 113 — SICOM devedor não-Fazenda (TCM_6)",
                "ate": CORTE_EC113,
                "metodo": "sicom_composto",
                "fonte": ("CJF", "TCM_6"),
                "incide_sobre": "principal_e_juros",
            },
            {
                "id": "R2",
                "rotulo": "EC 113 — Selic linear",
                "de": CORTE_EC113,
                "ate": CORTE_EC136,
                "metodo": "selic_linear",
                "fonte": ("BACEN", 4390),
                "incide_sobre": "principal_e_juros",
            },
            {
                "id": "R3",
                "rotulo": "EC 136 — IPCA composto + juros 2% a.a.",
                "de": CORTE_EC136,
                "metodo": "ipca_composto_mais_juros",
                "fonte": ("BACEN", 433),
                "juros_aa": JUROS_EC136_AA,
                "incide_sobre": "principal",
            },
        ],
    },
    "federal_previdenciario": {
        "nome": "Precatório Federal — Benefício Previdenciário",
        "descricao": (
            "Categoria 2 (benefício previdenciário). Mesma lógica encadeada do "
            "segmentador (R_graça, R1 -> R2 -> R3, cortes EC 113 e EC 136/jul-2025), "
            "com o R1 usando a TCM_2 para a correção (índice próprio do "
            "previdenciário — INPC/IGP, distinto do IPCA-E das demais categorias) "
            "e a Tabela de Juros de Mora Geral (não-tributário)."
        ),
        "r1_correcao": "TCM_2",              # SICOM categoria 2 (previdenciário)
        "r1_juros": "JUROS_NAO_TRIBUTARIO",  # Tabela de Juros de Mora Geral (não-tributário)
        "regimes": [
            {
                "id": "R1",
                "rotulo": "Pré-EC 113 — SICOM previdenciário (TCM_2)",
                "ate": CORTE_EC113,
                "metodo": "sicom_composto",
                "fonte": ("CJF", "TCM_2"),
                "incide_sobre": "principal_e_juros",
            },
            {
                "id": "R2",
                "rotulo": "EC 113 — Selic linear",
                "de": CORTE_EC113,
                "ate": CORTE_EC136,
                "metodo": "selic_linear",
                "fonte": ("BACEN", 4390),
                "incide_sobre": "principal_e_juros",
            },
            {
                "id": "R3",
                "rotulo": "EC 136 — IPCA composto + juros 2% a.a.",
                "de": CORTE_EC136,
                "metodo": "ipca_composto_mais_juros",
                "fonte": ("BACEN", 433),
                "juros_aa": JUROS_EC136_AA,
                "incide_sobre": "principal",
            },
        ],
    },
    "federal_desap": {
        "nome": "Precatório Federal — Desapropriações",
        "descricao": (
            "Categoria 1 (desapropriações). Com o precatório JÁ EXPEDIDO "
            "(precatorio_expedido=True), segue exatamente a mesma lógica do perfil "
            "federal_fazenda_geral (segmentador: R_graça, R1 -> R2 -> R3; nos dados "
            "atuais a TCM_1 é idêntica à TCM_5). Quando o precatório NÃO foi expedido, "
            "incidem JUROS COMPENSATÓRIOS simples sobre o principal corrigido dentro "
            "do R1, no regime definido por transito_antes_adi2332."
        ),
        "r1_correcao": "TCM_1",              # SICOM categoria 1 (desapropriações)
        "r1_juros": "JUROS_NAO_TRIBUTARIO",  # Tabela de Juros de Mora Geral
        "juros_compensatorios": True,        # habilita compensatórios se não expedido
        "regimes": [
            {
                "id": "R1",
                "rotulo": "Pré-EC 113 — SICOM desapropriação (TCM_1) + compensatórios",
                "ate": CORTE_EC113,
                "metodo": "sicom_composto",
                "fonte": ("CJF", "TCM_1"),
                "incide_sobre": "principal_e_juros",
            },
            {
                "id": "R2",
                "rotulo": "EC 113 — Selic linear",
                "de": CORTE_EC113,
                "ate": CORTE_EC136,
                "metodo": "selic_linear",
                "fonte": ("BACEN", 4390),
                "incide_sobre": "principal_e_juros",
            },
            {
                "id": "R3",
                "rotulo": "EC 136 — IPCA composto + juros 2% a.a.",
                "de": CORTE_EC136,
                "metodo": "ipca_composto_mais_juros",
                "fonte": ("BACEN", 433),
                "juros_aa": JUROS_EC136_AA,
                "incide_sobre": "principal",
            },
        ],
    },
    "federal_tributario": {
        "nome": "Precatório Federal — Repetição de Indébito Tributário",
        "descricao": (
            "Regime PRÓPRIO do indébito tributário — NÃO usa período de graça nem "
            "os cortes EC113/EC136 (a Selic já é o índice desde 01/1996). "
            "Até 01/1996: correção monetária composta (TCM_4, referência 01/1996) "
            "+ juros de mora (JUROS_TRIBUTARIO), sobre principal e juros. "
            "A partir de 01/1996: Selic LINEAR (BACEN 4390) incidindo SOMENTE sobre "
            "o principal; o componente de juros fica fixo. Corre até o depósito/hoje."
        ),
        "motor": "tributario",           # usa _calcular_tributario (escape do segmentador)
        "r1_correcao": "TCM_4",          # correção pré-1996 (referência 01/1996)
        "r1_juros": "JUROS_TRIBUTARIO",  # juros de mora pré-1996
    },
}


def obter_perfil(chave: str) -> dict:
    """Retorna o perfil pela chave ou levanta KeyError com mensagem clara."""
    try:
        return PERFIS[chave]
    except KeyError as e:
        disponiveis = ", ".join(sorted(PERFIS)) or "(nenhum)"
        raise KeyError(
            f"Perfil '{chave}' não existe. Disponíveis: {disponiveis}."
        ) from e
