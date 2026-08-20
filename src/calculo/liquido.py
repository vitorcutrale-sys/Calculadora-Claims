"""
Camada de Valor de Face Líquido — Fase 5.

Parte do Valor de Face Bruto (motor.calcular_vf_bruto) e aplica os descontos
que reduzem o que efetivamente se recebe.

Os descontos NÃO são iguais para todo tipo de precatório. São DUAS camadas
tributárias mutuamente exclusivas, definidas pelo tipo de verba:

- **Verba remuneratória** (perfis com `verba_remuneratoria: True` — hoje só
  `federal_servidores`): PSS + IR-RRA. Não incide o IRRF de 3%.

- **Demais perfis**: IRRF de **3% sobre o montante global** (principal + juros
  + selic), art. 27 da Lei 10.833/2003, sem quaisquer deduções. Não incidem
  PSS nem IR-RRA.

Sobre o valor após impostos incide a **reserva de honorários**, cuja base
depende do destacamento:
  destacados     -> % sobre o valor BRUTO
  não destacados -> % sobre o valor LÍQUIDO dos impostos

Por fim, o percentual do ativo (share) incide sobre o Total Cedível.

Vocabulário do resultado (espelha a planilha-protótipo):
  vf_liquido    = VF Bruto − impostos            (sem honorários)
  total_cedivel = vf_liquido − honorários
  total_share   = total_cedivel × percentual_ativo

Implementado nesta etapa:
    PSS    = alíquota_efetiva × PRINCIPAL corrigido
    IR-RRA = nº_meses × (base_mensal × alíquota − dedução)
             onde base_mensal = (PRINCIPAL corrigido − PSS) / nº_meses

O IR-RRA (Rendimentos Recebidos Acumuladamente, art. 12-A da Lei 7.713/88)
usa a tabela progressiva MENSAL vigente, aplicada sobre a média mensal do
rendimento — por isso a base é dividida pelo número de meses a que o crédito
se refere e o imposto é multiplicado de volta por esse número.

Incide apenas sobre o principal líquido de PSS: os juros de mora de verba
remuneratória não entram na base (natureza indenizatória).

A isenção da Lei 15.191/2025 (rendimentos até R$ 5 mil) NÃO se aplica aqui —
trata-se de rendimento recebido acumuladamente, com tributação própria.

O PSS é uma alíquota informada caso a caso (não é fixa) e incide apenas sobre
o componente de principal do Valor de Face Bruto — não sobre juros nem sobre
a parcela de Selic.

A alíquota pode entrar de duas formas (ver PremissasAtivo.pss_aliquota_efetiva):
  - `pss_percentual` — a alíquota direta (ex.: 0.11);
  - `pss_valor`      — o PSS em R$ da conta na data base; a alíquota é derivada
                       como pss_valor / valor_principal e então aplicada ao
                       principal corrigido (mesma lógica).

Tudo em Decimal.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from .. import db as _db
from .modelo import PremissasAtivo
from .motor import ResultadoVF
from .perfis import IRRF_PRECATORIO_FEDERAL, obter_perfil

CEM = Decimal("100")


@dataclass
class ResultadoLiquido:
    """Valor de Face Bruto, descontos aplicados e Valor de Face Líquido."""
    vf_bruto: Decimal
    principal: Decimal          # componente de principal corrigido
    juros: Decimal              # componente de juros
    selic: Decimal              # componente de Selic (regime EC 113)
    pss: Decimal                # desconto de PSS (magnitude positiva)
    ir_rra: Decimal             # desconto de IR-RRA (magnitude positiva)
    irrf_global: Decimal        # IRRF de 3% sobre o global (perfis não remuneratórios)
    vf_liquido: Decimal         # VF Bruto menos os impostos (SEM honorários)
    honorarios: Decimal         # reserva de honorários (magnitude positiva)
    total_cedivel: Decimal      # VF Líquido menos os honorários
    total_share: Decimal        # Total Cedível × percentual do ativo
    data_final: date
    descontos: list = field(default_factory=list)   # auditoria linha a linha

    @property
    def principal_liquido_pss(self) -> Decimal:
        """Principal após o desconto de PSS — base do IR-RRA."""
        return self.principal - self.pss

    @property
    def impostos(self) -> Decimal:
        """Total de impostos/contribuições (PSS + IR-RRA + IRRF)."""
        return self.pss + self.ir_rra + self.irrf_global


def _faixa_irpf(
    conn: sqlite3.Connection, base_mensal: Decimal, quando: date
) -> sqlite3.Row:
    """
    Faixa da tabela progressiva mensal vigente em `quando` que contém
    `base_mensal`. Levanta ValueError se a tabela não estiver no banco.
    """
    faixas = _db.tabela_irpf_vigente(conn, quando.isoformat())
    if not faixas:
        raise ValueError(
            f"Tabela do IRPF não encontrada para {quando:%Y-%m-%d}. "
            f"Rode: python coletar_indices.py --so-irpf"
        )
    for f in faixas:
        limite = f["base_ate"]
        if limite is None or base_mensal <= Decimal(str(limite)):
            return f
    return faixas[-1]   # acima da última faixa


def calcular_vf_liquido(
    conn: sqlite3.Connection,
    resultado: ResultadoVF,
    premissas: PremissasAtivo,
    data_tabela_irpf: Optional[date] = None,
) -> ResultadoLiquido:
    """
    Aplica os descontos sobre o Valor de Face Bruto.

    Nesta etapa, PSS e IR-RRA — e somente para perfis de verba remuneratória
    (`verba_remuneratoria: True`). Em perfis não remuneratórios ambos são
    ignorados, mesmo que os inputs venham preenchidos.

    `data_tabela_irpf` escolhe a vigência da tabela do IRPF a usar
    (padrão: a data final do cálculo).
    """
    perfil = obter_perfil(premissas.perfil)
    descontos: list = []
    remuneratoria = bool(perfil.get("verba_remuneratoria"))

    aliquota = premissas.pss_aliquota_efetiva
    origem = premissas.pss_origem

    pss = Decimal("0")
    if remuneratoria:
        if aliquota > 0:
            pss = resultado.principal * aliquota
            linha = {
                "desconto": "PSS",
                "base": "principal corrigido",
                "valor_base": resultado.principal,
                "aliquota": aliquota,
                "origem_aliquota": origem,
                "valor": pss,
            }
            if origem == "valor":
                # Deixa rastreável de onde veio a alíquota derivada.
                linha["pss_valor_informado"] = premissas.pss_valor
                linha["principal_nominal"] = premissas.valor_principal
            descontos.append(linha)
    elif aliquota > 0:
        descontos.append({
            "desconto": "PSS",
            "base": "n/a",
            "aliquota": aliquota,
            "origem_aliquota": origem,
            "valor": Decimal("0"),
            "observacao": (
                f"ignorado: perfil '{premissas.perfil}' não é de verba remuneratória"
            ),
        })

    # ---------------- IR-RRA ---------------- #
    # Base: principal corrigido líquido de PSS, dividido pelo nº de meses
    # a que o crédito se refere. O imposto é calculado sobre essa média
    # mensal e multiplicado de volta pelo nº de meses.
    ir_rra = Decimal("0")
    base_ir = resultado.principal - pss
    if remuneratoria and premissas.meses_ir_rra > 0 and base_ir > 0:
        meses = Decimal(premissas.meses_ir_rra)
        base_mensal = base_ir / meses
        data_ref = data_tabela_irpf or resultado.data_final
        faixa = _faixa_irpf(conn, base_mensal, data_ref)

        aliquota_ir = Decimal(str(faixa["aliquota"])) / CEM
        deducao = Decimal(str(faixa["deducao"]))
        ir_mensal = base_mensal * aliquota_ir - deducao
        if ir_mensal < 0:
            ir_mensal = Decimal("0")
        ir_rra = ir_mensal * meses

        descontos.append({
            "desconto": "IR-RRA",
            "base": "principal corrigido líquido de PSS",
            "valor_base": base_ir,
            "meses": premissas.meses_ir_rra,
            "base_mensal": base_mensal,
            "tabela_vigencia": faixa["vigencia_inicio"],
            "faixa": faixa["ordem"],
            "aliquota": aliquota_ir,
            "deducao": deducao,
            "valor": ir_rra,
        })
    elif remuneratoria and premissas.meses_ir_rra == 0:
        descontos.append({
            "desconto": "IR-RRA",
            "base": "n/a",
            "valor": Decimal("0"),
            "observacao": "não calculado: meses_ir_rra = 0",
        })

    # ---------------- IRRF de 3% sobre o global ---------------- #
    # Só para perfis NÃO remuneratórios: art. 27 da Lei 10.833/2003 —
    # 3% sobre o montante pago (principal + juros + selic), sem deduções.
    # `aliquota_imposto` permite sobrepor a alíquota legal quando informada.
    irrf_global = Decimal("0")
    if not remuneratoria:
        aliquota_irrf = (
            premissas.aliquota_imposto
            if premissas.aliquota_imposto > 0
            else IRRF_PRECATORIO_FEDERAL
        )
        irrf_global = resultado.saldo * aliquota_irrf
        descontos.append({
            "desconto": "IRRF (art. 27 Lei 10.833/2003)",
            "base": "montante global (principal + juros + selic)",
            "valor_base": resultado.saldo,
            "aliquota": aliquota_irrf,
            "origem_aliquota": (
                "informada" if premissas.aliquota_imposto > 0 else "legal (3%)"
            ),
            "valor": irrf_global,
        })
    elif premissas.aliquota_imposto > 0:
        descontos.append({
            "desconto": "IRRF (art. 27 Lei 10.833/2003)",
            "base": "n/a",
            "aliquota": premissas.aliquota_imposto,
            "valor": Decimal("0"),
            "observacao": (
                f"ignorado: perfil '{premissas.perfil}' é de verba remuneratória "
                f"(aplica-se PSS + IR-RRA)"
            ),
        })

    # ---------------- Reserva de honorários ---------------- #
    # A base depende do destacamento:
    #   destacados     -> % sobre o valor BRUTO (impostos também sobre o bruto)
    #   não destacados -> % sobre o valor LÍQUIDO dos impostos
    impostos = pss + ir_rra + irrf_global
    vf_liquido = resultado.saldo - impostos

    honorarios = Decimal("0")
    if premissas.honorarios_percentual > 0:
        if premissas.destacamento_honorarios:
            base_hon = resultado.saldo
            rotulo_base = "valor bruto (honorários destacados)"
        else:
            base_hon = vf_liquido
            rotulo_base = "valor líquido dos impostos (honorários não destacados)"
        honorarios = base_hon * premissas.honorarios_percentual
        descontos.append({
            "desconto": "Honorários",
            "base": rotulo_base,
            "valor_base": base_hon,
            "aliquota": premissas.honorarios_percentual,
            "destacados": premissas.destacamento_honorarios,
            "valor": honorarios,
        })

    # ---------------- Percentual do ativo (share) ---------------- #
    # Proporção do ativo efetivamente adquirida/considerada, aplicada
    # sobre o Total Cedível.
    total_cedivel = vf_liquido - honorarios
    total_share = total_cedivel * premissas.percentual_ativo
    if premissas.percentual_ativo != Decimal("1"):
        descontos.append({
            "desconto": "Share do ativo",
            "base": "total cedível",
            "valor_base": total_cedivel,
            "aliquota": premissas.percentual_ativo,
            "valor": total_cedivel - total_share,
        })

    return ResultadoLiquido(
        vf_bruto=resultado.saldo,
        principal=resultado.principal,
        juros=resultado.juros,
        selic=resultado.selic,
        pss=pss,
        ir_rra=ir_rra,
        irrf_global=irrf_global,
        vf_liquido=vf_liquido,
        honorarios=honorarios,
        total_cedivel=total_cedivel,
        total_share=total_share,
        data_final=resultado.data_final,
        descontos=descontos,
    )
