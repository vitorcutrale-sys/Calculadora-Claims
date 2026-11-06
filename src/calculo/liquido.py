"""
Camada de Valor de Face Líquido — Fase 5.

Parte do valor atualizado do crédito (motor.calcular_vf_bruto), aplica o
share do fundo e, sobre a parte do fundo, os descontos que reduzem o que
efetivamente se recebe.

**Share do fundo.** O percentual do ativo é o percentual do precatório que o
fundo detém. Ele incide PRIMEIRO, sobre o valor atualizado, e define o VF Bruto:

    VF Bruto = valor atualizado do crédito × share

Todos os descontos partem desse VF Bruto, isto é, da base proporcional ao
share. A única exceção opcional é o IR-RRA (ver abaixo).

Os descontos NÃO são iguais para todo tipo de precatório. São DUAS camadas
tributárias mutuamente exclusivas, definidas pelo tipo de verba:

- **Verba remuneratória** (perfis com `verba_remuneratoria: True` — hoje só
  `federal_servidores`): PSS + IR-RRA. Não incide o IRRF de 3%.

- **Demais perfis**: IRRF de **3% sobre o montante global** (principal + juros
  + selic), art. 27 da Lei 10.833/2003, sem quaisquer deduções. Não incidem
  PSS nem IR-RRA. Por ser alíquota fixa, incide sempre sobre a base
  proporcional ao share.

A **reserva de honorários** muda a ORDEM da cascata conforme o destacamento:

  destacados     -> share -> honorários -> impostos
                    os honorários (% sobre o VF Bruto) saem ANTES dos impostos,
                    e os impostos incidem sobre a base já sem eles: cada
                    componente (principal, juros, selic) é reduzido na mesma
                    proporção dos honorários
  não destacados -> share -> impostos -> honorários
                    os impostos incidem sobre o VF Bruto e os honorários, sobre
                    o que sobra deles

Vocabulário do resultado:
  valor_atualizado = valor do crédito inteiro corrigido (100%)
  vf_bruto         = valor_atualizado × share
  base_impostos    = VF Bruto − honorários destacados (= VF Bruto se não destacados)
  vf_liquido       = VF Bruto − impostos            (sem honorários)
  total_cedivel    = VF Bruto − impostos − honorários

Implementado nesta etapa:
    PSS    = alíquota_efetiva × PRINCIPAL corrigido proporcional ao share
             (líquido dos honorários destacados)
    IR-RRA = nº_meses × (base_mensal × alíquota − dedução)
             onde base_mensal = (PRINCIPAL corrigido − PSS) / nº_meses,
             também líquido dos honorários destacados

O IR-RRA (Rendimentos Recebidos Acumuladamente, art. 12-A da Lei 7.713/88)
usa a tabela progressiva MENSAL vigente, aplicada sobre a média mensal do
rendimento — por isso a base é dividida pelo número de meses a que o crédito
se refere e o imposto é multiplicado de volta por esse número.

Como a tabela é progressiva, importa sobre QUAL principal ela é aplicada
(PremissasAtivo.ir_rra_base):
  integral     -> sobre o principal do crédito inteiro; o fundo arca com a
                  fração do seu share no imposto apurado
  proporcional -> diretamente sobre o principal proporcional ao share

Incide apenas sobre o principal líquido de PSS: os juros de mora de verba
remuneratória não entram na base (natureza indenizatória).

A isenção da Lei 15.191/2025 (rendimentos até R$ 5 mil) NÃO se aplica aqui —
trata-se de rendimento recebido acumuladamente, com tributação própria.

O PSS é uma alíquota informada caso a caso (não é fixa) e incide apenas sobre
o componente de principal — não sobre juros nem sobre a parcela de Selic.

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
    """Valor atualizado, VF Bruto (share), descontos e Valor de Face Líquido."""
    valor_atualizado: Decimal   # crédito inteiro corrigido (100%)
    percentual_ativo: Decimal   # share do fundo (fração)
    vf_bruto: Decimal           # valor atualizado × share
    honorarios_destacados: bool # True: honorários saem antes dos impostos
    base_impostos: Decimal      # VF Bruto − honorários destacados
    principal: Decimal          # componente de principal corrigido (crédito inteiro)
    juros: Decimal              # componente de juros (crédito inteiro)
    selic: Decimal              # componente de Selic (regime EC 113, crédito inteiro)
    pss: Decimal                # desconto de PSS (magnitude positiva)
    ir_rra: Decimal             # desconto de IR-RRA (magnitude positiva)
    irrf_global: Decimal        # IRRF de 3% sobre o global (perfis não remuneratórios)
    vf_liquido: Decimal         # VF Bruto menos os impostos (SEM honorários)
    honorarios: Decimal         # reserva de honorários (magnitude positiva)
    total_cedivel: Decimal      # VF Líquido menos os honorários
    data_final: date
    descontos: list = field(default_factory=list)   # auditoria linha a linha

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


def _ir_rra(
    conn: sqlite3.Connection, base: Decimal, meses: int, quando: date
) -> tuple[Decimal, dict]:
    """IR-RRA sobre `base` repartida em `meses`, pela tabela vigente em `quando`."""
    n = Decimal(meses)
    base_mensal = base / n
    faixa = _faixa_irpf(conn, base_mensal, quando)
    aliquota = Decimal(str(faixa["aliquota"])) / CEM
    deducao = Decimal(str(faixa["deducao"]))
    ir_mensal = max(base_mensal * aliquota - deducao, Decimal("0"))
    return ir_mensal * n, {
        "meses": meses,
        "base_mensal": base_mensal,
        "tabela_vigencia": faixa["vigencia_inicio"],
        "faixa": faixa["ordem"],
        "aliquota": aliquota,
        "deducao": deducao,
    }


def calcular_vf_liquido(
    conn: sqlite3.Connection,
    resultado: ResultadoVF,
    premissas: PremissasAtivo,
    data_tabela_irpf: Optional[date] = None,
) -> ResultadoLiquido:
    """
    Aplica o share e os descontos sobre o valor atualizado do crédito.

    PSS e IR-RRA só para perfis de verba remuneratória
    (`verba_remuneratoria: True`). Em perfis não remuneratórios ambos são
    ignorados, mesmo que os inputs venham preenchidos.

    `data_tabela_irpf` escolhe a vigência da tabela do IRPF a usar
    (padrão: a data final do cálculo).
    """
    perfil = obter_perfil(premissas.perfil)
    descontos: list = []
    remuneratoria = bool(perfil.get("verba_remuneratoria"))

    # ---------------- Share do fundo -> VF Bruto ---------------- #
    share = premissas.percentual_ativo
    valor_atualizado = resultado.saldo
    vf_bruto = valor_atualizado * share

    # ---------------- Honorários destacados ---------------- #
    # Saem do share ANTES dos impostos. O que sobra (fração `liquido_hon`) é
    # a base dos impostos, componente a componente.
    destacados = premissas.destacamento_honorarios
    honorarios = Decimal("0")
    liquido_hon = Decimal("1")
    if destacados and premissas.honorarios_percentual > 0:
        honorarios = vf_bruto * premissas.honorarios_percentual
        liquido_hon = 1 - premissas.honorarios_percentual
        descontos.append({
            "desconto": "Honorários",
            "base": "VF Bruto (honorários destacados, antes dos impostos)",
            "valor_base": vf_bruto,
            "aliquota": premissas.honorarios_percentual,
            "destacados": True,
            "valor": honorarios,
        })
    base_impostos = vf_bruto * liquido_hon
    principal_share = resultado.principal * share * liquido_hon
    sufixo = " líquido dos honorários destacados" if liquido_hon != 1 else ""

    aliquota = premissas.pss_aliquota_efetiva
    origem = premissas.pss_origem

    # ---------------- PSS ---------------- #
    # Linear: sobre a parte do fundo no principal corrigido.
    pss = Decimal("0")
    if remuneratoria:
        if aliquota > 0:
            pss = principal_share * aliquota
            linha = {
                "desconto": "PSS",
                "base": "principal corrigido proporcional ao share" + sufixo,
                "valor_base": principal_share,
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
    #   integral     -> tabela aplicada ao crédito inteiro; o fundo arca com
    #                   o share do imposto apurado
    #   proporcional -> tabela aplicada direto à parte do fundo
    ir_rra = Decimal("0")
    base_ir_integral = (
        resultado.principal * (1 - aliquota) * liquido_hon if remuneratoria else Decimal("0")
    )
    if remuneratoria and premissas.meses_ir_rra > 0 and base_ir_integral > 0:
        data_ref = data_tabela_irpf or resultado.data_final
        if premissas.ir_rra_base == "integral":
            ir_credito, det = _ir_rra(conn, base_ir_integral, premissas.meses_ir_rra, data_ref)
            ir_rra = ir_credito * share
            linha = {
                "desconto": "IR-RRA",
                "base": "principal corrigido líquido de PSS — crédito integral" + sufixo,
                "valor_base": base_ir_integral,
                "ir_rra_base": "integral",
                **det,
                "ir_credito_integral": ir_credito,
                "share": share,
                "valor": ir_rra,
            }
        else:
            base_ir = base_ir_integral * share
            ir_rra, det = _ir_rra(conn, base_ir, premissas.meses_ir_rra, data_ref)
            linha = {
                "desconto": "IR-RRA",
                "base": "principal corrigido líquido de PSS — proporcional ao share" + sufixo,
                "valor_base": base_ir,
                "ir_rra_base": "proporcional",
                **det,
                "valor": ir_rra,
            }
        descontos.append(linha)
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
    # Alíquota fixa: incide sempre sobre a base proporcional (o VF Bruto,
    # líquido dos honorários destacados).
    # `aliquota_imposto` permite sobrepor a alíquota legal quando informada.
    irrf_global = Decimal("0")
    if not remuneratoria:
        # Alíquota informada vence sempre. Na ausência dela, aplica-se a
        # alíquota legal de 3% apenas nos perfis sujeitos à retenção
        # automática — o depósito judicial, por exemplo, não é.
        if premissas.aliquota_imposto > 0:
            aliquota_irrf = premissas.aliquota_imposto
            origem_irrf = "informada"
        elif perfil.get("irrf_automatico", True):
            aliquota_irrf = IRRF_PRECATORIO_FEDERAL
            origem_irrf = "legal (3%)"
        else:
            aliquota_irrf = Decimal("0")
            origem_irrf = "sem retenção automática neste perfil"

        if aliquota_irrf > 0:
            irrf_global = base_impostos * aliquota_irrf
            descontos.append({
                "desconto": "IRRF (art. 27 Lei 10.833/2003)",
                "base": "montante global proporcional ao share (VF Bruto)" + sufixo,
                "valor_base": base_impostos,
                "aliquota": aliquota_irrf,
                "origem_aliquota": origem_irrf,
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

    impostos = pss + ir_rra + irrf_global
    vf_liquido = vf_bruto - impostos

    # ---------------- Honorários não destacados ---------------- #
    # Saem DEPOIS dos impostos, sobre o que sobra deles. (Os destacados já
    # saíram lá em cima, antes dos impostos.)
    if not destacados and premissas.honorarios_percentual > 0:
        honorarios = vf_liquido * premissas.honorarios_percentual
        descontos.append({
            "desconto": "Honorários",
            "base": "VF Bruto líquido dos impostos (honorários não destacados)",
            "valor_base": vf_liquido,
            "aliquota": premissas.honorarios_percentual,
            "destacados": False,
            "valor": honorarios,
        })

    return ResultadoLiquido(
        valor_atualizado=valor_atualizado,
        percentual_ativo=share,
        vf_bruto=vf_bruto,
        honorarios_destacados=destacados,
        base_impostos=base_impostos,
        principal=resultado.principal,
        juros=resultado.juros,
        selic=resultado.selic,
        pss=pss,
        ir_rra=ir_rra,
        irrf_global=irrf_global,
        vf_liquido=vf_liquido,
        honorarios=honorarios,
        total_cedivel=vf_liquido - honorarios,
        data_final=resultado.data_final,
        descontos=descontos,
    )
