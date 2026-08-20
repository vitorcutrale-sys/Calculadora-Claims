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
        pss_percentual    : alíquota do PSS (contribuição previdenciária) como
                            fração — ex.: 0.11 para 11%. NÃO é fixa: varia por
                            caso. Incide sobre o PRINCIPAL corrigido.
                            Só se aplica a perfis de verba remuneratória
                            (hoje: federal_servidores).
        pss_valor         : ALTERNATIVA ao pss_percentual — o PSS em R$, como
                            consta da conta na data base. A alíquota é derivada
                            (pss_valor / valor_principal) e daí em diante segue
                            a mesma lógica: incide sobre o principal corrigido.
                            Informe pss_percentual OU pss_valor, nunca os dois.
        meses_ir_rra      : nº de meses para o cálculo do IR-RRA
        aliquota_imposto  : alíquota do IRRF sobre o montante global (fração).
                            Só se aplica a perfis NÃO remuneratórios; quando
                            deixada em 0, usa-se a alíquota legal de 3%
                            (art. 27 da Lei 10.833/2003). Informe apenas para
                            sobrepor essa alíquota em casos específicos.
        percentual_ativo  : percentual do ativo adquirido/considerado (fração)
        honorarios_percentual : percentual de honorários sobre o crédito, como
                            fração (ex.: 0.20 para 20%). A BASE depende de
                            `destacamento_honorarios`.
        destacamento_honorarios : honorários destacados? (binária Sim/Não = 1/0).
                            Define a BASE da reserva de honorários:
                              True  (destacados)     -> % sobre o valor BRUTO
                                    (os impostos/contribuições também incidem
                                     sobre o bruto)
                              False (não destacados) -> % sobre o valor LÍQUIDO
                                    dos impostos (após PSS+IR-RRA ou IRRF 3%)
                            NÃO altera a curva bruta pré-expedição (só atua na
                            camada de valor líquido).
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
    pss_percentual: Decimal = Decimal("0")
    pss_valor: Decimal = Decimal("0")
    meses_ir_rra: int = 0
    aliquota_imposto: Decimal = Decimal("0")
    percentual_ativo: Decimal = Decimal("1")
    honorarios_percentual: Decimal = Decimal("0")
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

    @property
    def pss_aliquota_efetiva(self) -> Decimal:
        """
        Alíquota de PSS a aplicar sobre o principal corrigido.

        - `pss_percentual` informado -> usa direto.
        - `pss_valor` (R$) informado -> deriva a alíquota que esse valor
          representa do principal nominal da data base:
              pss_valor / valor_principal
        - nenhum dos dois -> zero.
        """
        if self.pss_percentual > 0:
            return self.pss_percentual
        if self.pss_valor > 0 and self.valor_principal > 0:
            return self.pss_valor / self.valor_principal
        return Decimal("0")

    @property
    def pss_origem(self) -> str:
        """Como a alíquota de PSS foi obtida: 'percentual', 'valor' ou 'nenhum'."""
        if self.pss_percentual > 0:
            return "percentual"
        if self.pss_valor > 0:
            return "valor"
        return "nenhum"

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
        if not (Decimal("0") <= self.pss_percentual <= Decimal("1")):
            raise ValueError("pss_percentual deve estar entre 0 e 1 (fração).")
        if self.pss_valor < 0:
            raise ValueError("pss_valor não pode ser negativo.")
        if self.pss_percentual > 0 and self.pss_valor > 0:
            raise ValueError(
                "Informe pss_percentual OU pss_valor, não os dois "
                f"(recebido: {self.pss_percentual} e R$ {self.pss_valor})."
            )
        if self.pss_valor > 0:
            if self.valor_principal <= 0:
                raise ValueError(
                    "pss_valor exige valor_principal > 0 para derivar a alíquota."
                )
            if self.pss_valor > self.valor_principal:
                raise ValueError(
                    f"pss_valor (R$ {self.pss_valor}) não pode ser maior que o "
                    f"principal (R$ {self.valor_principal})."
                )
        if self.meses_ir_rra < 0:
            raise ValueError("meses_ir_rra não pode ser negativo.")
        if not (Decimal("0") <= self.aliquota_imposto <= Decimal("1")):
            raise ValueError("aliquota_imposto deve estar entre 0 e 1 (fração).")
        if not (Decimal("0") <= self.percentual_ativo <= Decimal("1")):
            raise ValueError("percentual_ativo deve estar entre 0 e 1 (fração).")
        if not (Decimal("0") <= self.honorarios_percentual <= Decimal("1")):
            raise ValueError(
                "honorarios_percentual deve estar entre 0 e 1 (fração)."
            )
        # Obs.: a data base pode coincidir com o início da graça (valor "as of"
        # a expedição) ou ser anterior a ela (crédito avaliado na origem). Por
        # isso NÃO exigimos data_base <= expedição.
