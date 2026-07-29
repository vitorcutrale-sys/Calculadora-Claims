"""
Modelo de input (premissas do ativo) e regras de datas — Fase 5, bloco 1.

Bloco 1 = Precatório Federal de Servidores e Empregados Públicos.

Este módulo define:
- `PremissasAtivo`: os inputs que o usuário fornece para um ativo.
- `expedicao_do_ano_orcamento(ano)`: data de corte (fim da curva de correção),
  derivada do ano orçamentário.
- `fim_loa(ano)`: data prevista de pagamento (31/12 do ano orçamentário).

Decisões de domínio (confirmadas com o usuário):
- A expedição (data de corte da correção) é o ÚLTIMO DIA DO MÊS ANTERIOR ao
  prazo de apresentação, que mudou ao longo do tempo:
      ano orçamento <= 2022   -> 30/06 do ano anterior  (prazo era julho)
      2023 a 2026             -> 31/03 do ano anterior  (prazo passou a abril)
      ano orçamento >= 2027   -> 31/01 do ano anterior  (prazo passou a fevereiro)
- O fim da LOA (pagamento) é sempre 31/12 do ano orçamentário.

Todo valor monetário é Decimal; toda data é datetime.date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


# ------------------------------------------------------------------ #
# Regras de datas derivadas do ano orçamentário                       #
# ------------------------------------------------------------------ #
def expedicao_do_ano_orcamento(ano_orcamento: int) -> date:
    """
    Data de corte (fim da curva de correção) derivada do ano orçamentário.

    Retorna o último dia do mês anterior ao prazo de apresentação vigente:
        <= 2022     -> 30/06 do ano anterior
        2023..2026  -> 31/03 do ano anterior
        >= 2027     -> 31/01 do ano anterior

    >>> expedicao_do_ano_orcamento(2022)
    datetime.date(2021, 6, 30)
    >>> expedicao_do_ano_orcamento(2026)
    datetime.date(2025, 3, 31)
    >>> expedicao_do_ano_orcamento(2027)
    datetime.date(2026, 1, 31)
    """
    ano_anterior = ano_orcamento - 1
    if ano_orcamento <= 2022:
        return date(ano_anterior, 6, 30)   # fim de junho (prazo: julho)
    if ano_orcamento <= 2026:
        return date(ano_anterior, 3, 31)   # fim de março (prazo: abril)
    return date(ano_anterior, 1, 31)       # fim de janeiro (prazo: fevereiro)


def fim_loa(ano_orcamento: int) -> date:
    """
    Data prevista de pagamento do precatório: 31/12 do ano orçamentário.

    >>> fim_loa(2026)
    datetime.date(2026, 12, 31)
    """
    return date(ano_orcamento, 12, 31)


# ------------------------------------------------------------------ #
# Premissas do ativo (inputs do bloco 1)                              #
# ------------------------------------------------------------------ #
@dataclass(frozen=True)
class PremissasAtivo:
    """
    Inputs de um Precatório Federal de Servidores (bloco 1).

    Campos monetários em Decimal (R$); percentuais em Decimal fracionário
    (ex.: 0.20 para 20%).

    Atributos:
        valor_principal   : valor do principal na data base (R$)
        valor_juros       : valor dos juros na data base (R$)
        data_base         : data base do crédito
        ano_orcamento     : ano orçamentário do precatório (define a expedição)
        pss_principal     : PSS (contribuição previdenciária) sobre o principal (R$)
        meses_ir_rra      : nº de meses para o cálculo do IR-RRA
        aliquota_imposto  : alíquota fixa de imposto (fração, ex.: 0.03)
        percentual_ativo  : percentual do ativo adquirido/considerado (fração)
        destacamento_honorarios : honorários destacados? (binária Sim/Não = 1/0).
                            Define a BASE da reserva de honorários, aplicada APÓS
                            o cálculo da curva de valor de face BRUTA:
                              True  (destacados)     -> honorários sobre o BRUTO
                              False (não destacados) -> desconto sobre o LÍQUIDO
                            NÃO altera a curva bruta pré-expedição (só é usada
                            depois, na camada de honorários/líquido).
        precatorio_expedido : o precatório já foi expedido? (Sim/Não = 1/0).
                            Usado no perfil de desapropriação: quando False (não
                            expedido), incidem JUROS COMPENSATÓRIOS dentro do R1.
                            Nos demais perfis não tem efeito.
        transito_antes_adi2332 : o título transitou em julgado ANTES da ADI 2332?
                            Só relevante quando precatorio_expedido=False:
                              True  -> compensatórios de 1% a.m. simples em todo o R1
                              False -> regime do Manual de Cálculos da JF:
                                       1% a.m. simples até 10/06/1997 e
                                       0,5% a.m. simples de 11/06/1997 a nov/2021
                                       (de dez/2021 em diante já embutidos na
                                       Selic / Taxa Legal — sem taxa adicional)
        perfil            : chave do perfil em perfis.PERFIS (default bloco 1)
    """

    valor_principal: Decimal
    valor_juros: Decimal
    data_base: date
    ano_orcamento: int
    pss_principal: Decimal = Decimal("0")
    meses_ir_rra: int = 0
    aliquota_imposto: Decimal = Decimal("0")
    percentual_ativo: Decimal = Decimal("1")
    destacamento_honorarios: bool = True
    precatorio_expedido: bool = True
    transito_antes_adi2332: bool = False
    perfil: str = "federal_servidores"

    # ---- propriedades derivadas ---- #
    @property
    def data_expedicao(self) -> date:
        """Data de corte da correção, derivada do ano orçamentário."""
        return expedicao_do_ano_orcamento(self.ano_orcamento)

    @property
    def data_fim_loa(self) -> date:
        """Data prevista de pagamento (31/12 do ano orçamentário)."""
        return fim_loa(self.ano_orcamento)

    # ---- validação ---- #
    def validar(self) -> None:
        """
        Valida consistência básica dos inputs. Levanta ValueError com
        mensagem clara no primeiro problema encontrado.
        """
        if self.valor_principal < 0:
            raise ValueError("valor_principal não pode ser negativo.")
        if self.valor_juros < 0:
            raise ValueError("valor_juros não pode ser negativo.")
        if self.pss_principal < 0:
            raise ValueError("pss_principal não pode ser negativo.")
        if self.meses_ir_rra < 0:
            raise ValueError("meses_ir_rra não pode ser negativo.")
        if not (Decimal("0") <= self.aliquota_imposto <= Decimal("1")):
            raise ValueError("aliquota_imposto deve estar entre 0 e 1 (fração).")
        if not (Decimal("0") <= self.percentual_ativo <= Decimal("1")):
            raise ValueError("percentual_ativo deve estar entre 0 e 1 (fração).")
        # Obs.: a data base pode coincidir com o início da graça (valor "as of"
        # a expedição) ou ser anterior a ela (crédito avaliado na origem). Por
        # isso NÃO exigimos data_base <= expedição.
