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
from typing import Optional


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


# Modos de base do IR-RRA (ver PremissasAtivo.ir_rra_base).
IR_RRA_BASES = ("integral", "proporcional")


# Composições possíveis do R3 (ver PremissasAtivo.r3_regime e motor).
R3_REGIMES = {"automatico", "ipca_2pct", "ipca15_taxa_legal"}


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
        ir_rra_base       : base sobre a qual a tabela progressiva do IR-RRA é
                            aplicada ("integral" ou "proporcional"):
                              integral     -> o IR é apurado sobre o principal
                                              do crédito INTEIRO (a faixa da
                                              tabela é a do crédito todo) e o
                                              fundo arca com a fração do seu
                                              share nesse imposto
                              proporcional -> o IR é apurado diretamente sobre
                                              o principal proporcional ao share
                            Como a tabela é progressiva, os dois modos dão
                            resultados diferentes sempre que o share < 100%.
                            O IRRF de alíquota fixa não tem essa opção: incide
                            sempre sobre a base proporcional.
        aliquota_imposto  : alíquota do IRRF sobre o montante global (fração).
                            Só se aplica a perfis NÃO remuneratórios; quando
                            deixada em 0, usa-se a alíquota legal de 3%
                            (art. 27 da Lei 10.833/2003). Informe apenas para
                            sobrepor essa alíquota em casos específicos.
        percentual_ativo  : SHARE do fundo — percentual do precatório que o
                            fundo detém (fração). Incide sobre o valor
                            atualizado do crédito e define o VF Bruto:
                                VF Bruto = valor atualizado × share
                            Todos os descontos partem desse VF Bruto.
        aplicar_juros_mora : incidir juros de mora? (default True). Desligado,
                            o motor não acrescenta os juros de mora do R1, os
                            juros do R3 (Taxa Legal ou 2% a.a.) nem os juros de
                            mora pré-1996 do tributário. A Selic do R2 e a do
                            tributário pós-1996 seguem aplicadas: é o índice
                            único da EC 113 e não separa correção de mora.
        data_base_juros   : data a partir da qual os juros de mora correm, se
                            diferente da data base (opcional). Pode ser
                            ANTERIOR à data base — os juros do intervalo são
                            apurados pelo regime de cada mês e somados ao
                            componente de juros — ou POSTERIOR, caso em que os
                            meses entre a data base e ela não geram juros.
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
                            Campo informativo do ativo. NÃO decide mais os juros
                            compensatórios — ver aplicar_juros_compensatorios.
        aplicar_juros_compensatorios : incidir JUROS COMPENSATÓRIOS sobre o
                            principal corrigido dentro do R1? (Sim/Não = 1/0).
                            Só tem efeito em perfis que os suportam (hoje:
                            federal_desap); informá-lo em outro perfil levanta
                            erro, para não passar em silêncio.

                            É **opt-in** (default False), e a razão é o formato
                            do input. Quando o precatório já está expedido e os
                            valores vêm de um OFÍCIO REQUISITÓRIO, os
                            compensatórios já estão embutidos no montante
                            requisitado — aplicá-los de novo contaria o mesmo
                            juro duas vezes e superavaliaria o ativo. Só se
                            marca True quando o principal informado ainda NÃO
                            os contempla (por exemplo, valores tirados da
                            sentença ou da conta de liquidação).

                            Note que isso é independente de precatorio_expedido:
                            um precatório expedido cujos valores não vêm de
                            ofício requisitório pode precisar dos compensatórios,
                            e um não expedido cujo laudo já os inclui, não.
        r3_regime         : qual composição usar no R3. Por padrão a escolha é
                            POSICIONAL (ver motor): antes da expedição vale
                            IPCA-E/IPCA-15 com juros da Taxa Legal; depois do
                            período de graça, IPCA com juros de 2% a.a.
                              "automatico"          -> regra posicional (padrão)
                              "ipca_2pct"           -> força IPCA + 2% a.a.
                              "ipca15_taxa_legal"   -> força IPCA-15 + Taxa Legal
                            Os dois últimos existem para casos em que a conta de
                            referência adota uma composição diferente da que a
                            posição na linha do tempo indicaria. Use com o motivo
                            registrado — é um desvio deliberado do padrão.
        transito_antes_adi2332 : o título transitou em julgado ANTES da ADI 2332?
                            Só relevante quando aplicar_juros_compensatorios=True:
                              True  -> compensatórios de 1% a.m. simples em todo o R1
                              False -> regime do Manual de Cálculos da JF:
                                       1% a.m. simples até 10/06/1997 e
                                       0,5% a.m. simples de 11/06/1997 a nov/2021
                                       (de dez/2021 em diante já embutidos na
                                       Selic / Taxa Legal — sem taxa adicional)
        precatorio_depositado : o valor do precatório já foi depositado?
                            Quando True, a correção pelos regimes judiciais
                            PARA na data do depósito e o valor passa a render
                            poupança por aniversário mensal (nos federais o
                            depósito é feito pelo valor BRUTO).
        data_deposito     : data do efetivo depósito. Obrigatória quando
                            `precatorio_depositado=True`.
        perfil            : chave do perfil em perfis.PERFIS (default bloco 1)
    """

    valor_principal: Decimal
    valor_juros: Decimal
    data_base: date
    ano_orcamento: int = 0     # não se aplica a perfis sem LOA (ver perfis.requer_loa)
    pss_percentual: Decimal = Decimal("0")
    pss_valor: Decimal = Decimal("0")
    meses_ir_rra: int = 0
    ir_rra_base: str = "integral"
    aliquota_imposto: Decimal = Decimal("0")
    percentual_ativo: Decimal = Decimal("1")
    aplicar_juros_mora: bool = True
    data_base_juros: Optional[date] = None
    honorarios_percentual: Decimal = Decimal("0")
    destacamento_honorarios: bool = True
    precatorio_expedido: bool = True
    aplicar_juros_compensatorios: bool = False
    transito_antes_adi2332: bool = False
    r3_regime: str = "automatico"
    precatorio_depositado: bool = False
    data_deposito: Optional[date] = None
    perfil: str = "federal_servidores"

    # ---- construtor de conveniência ---- #
    @classmethod
    def deposito_judicial(
        cls, montante: Decimal, data_deposito: date, **kwargs
    ) -> "PremissasAtivo":
        """
        Cria as premissas de um DEPÓSITO JUDICIAL a partir dos dois únicos
        inputs obrigatórios: o montante depositado e a data do depósito.

        O montante entra integralmente como principal e passa a render
        poupança a partir do depósito. Não há LOA, expedição nem regimes
        judiciais de correção.

        >>> p = PremissasAtivo.deposito_judicial(Decimal("500000"), date(2024, 3, 15))
        >>> p.perfil
        'deposito_judicial'
        """
        return cls(
            valor_principal=montante,
            valor_juros=Decimal("0"),
            data_base=data_deposito,
            precatorio_depositado=True,
            data_deposito=data_deposito,
            perfil="deposito_judicial",
            **kwargs,
        )

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
        if self.r3_regime not in R3_REGIMES:
            raise ValueError(
                f"r3_regime inválido: {self.r3_regime!r}. "
                f"Use um de {sorted(R3_REGIMES)}."
            )
        if self.meses_ir_rra < 0:
            raise ValueError("meses_ir_rra não pode ser negativo.")
        if self.ir_rra_base not in IR_RRA_BASES:
            raise ValueError(
                f"ir_rra_base deve ser {' ou '.join(IR_RRA_BASES)} "
                f"(recebido: {self.ir_rra_base!r})."
            )
        if not (Decimal("0") <= self.aliquota_imposto <= Decimal("1")):
            raise ValueError("aliquota_imposto deve estar entre 0 e 1 (fração).")
        if not (Decimal("0") <= self.percentual_ativo <= Decimal("1")):
            raise ValueError("percentual_ativo deve estar entre 0 e 1 (fração).")
        if not (Decimal("0") <= self.honorarios_percentual <= Decimal("1")):
            raise ValueError(
                "honorarios_percentual deve estar entre 0 e 1 (fração)."
            )
        # O ano orçamentário só faz sentido nos perfis atrelados a uma LOA.
        from .perfis import obter_perfil

        perfil = obter_perfil(self.perfil)
        if perfil.get("requer_loa", True) and self.ano_orcamento < 1988:
            raise ValueError(
                f"ano_orcamento ({self.ano_orcamento}) inválido para o perfil "
                f"'{self.perfil}', que depende do orçamento (LOA)."
            )
        # Regra independente da LOA: o depósito judicial parte da data do
        # depósito. Não estar atrelado à LOA (caso do tributário) não implica
        # exigir depósito.
        if perfil.get("motor") == "deposito_judicial" and self.data_deposito is None:
            raise ValueError(
                f"O perfil '{self.perfil}' exige data_deposito."
            )

        # Compensatórios são opt-in e só existem em perfis que os suportam.
        # Marcar o flag num perfil que os ignora é quase sempre erro de input,
        # então falha alto em vez de devolver um número silenciosamente menor.
        if self.aplicar_juros_compensatorios and not perfil.get("juros_compensatorios"):
            raise ValueError(
                f"aplicar_juros_compensatorios=True não é válido no perfil "
                f"'{self.perfil}': juros compensatórios só existem em perfis de "
                f"desapropriação."
            )

        if self.precatorio_depositado:
            if self.data_deposito is None:
                raise ValueError(
                    "data_deposito é obrigatória quando precatorio_depositado=True."
                )
            if self.data_deposito < self.data_base:
                raise ValueError(
                    f"data_deposito ({self.data_deposito}) é anterior à data base "
                    f"({self.data_base})."
                )
        # Obs.: a data base pode coincidir com o início da graça (valor "as of"
        # a expedição) ou ser anterior a ela (crédito avaliado na origem). Por
        # isso NÃO exigimos data_base <= expedição.
