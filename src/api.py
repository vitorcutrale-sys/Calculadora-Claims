"""
Camada de API da calculadora — contrato JSON entre a interface e o motor.

Recebe um dicionário (o corpo JSON enviado pela tela), monta as
`PremissasAtivo`, roda o motor (Valor de Face Bruto) e a camada de valor
líquido, e devolve um dicionário pronto para `json.dumps`.

Este módulo não sabe nada de HTTP: o `servidor.py` local o usa hoje, e o
handler do Lambda poderá usá-lo amanhã com o mesmo contrato.

Convenções do contrato:
- Valores monetários entram e saem como STRING com ponto decimal
  ("1234567.89"), nunca como número JSON — para não passar por float.
- Percentuais entram em PONTOS PERCENTUAIS, em campos terminados em `_pct`
  ("11" = 11%). A conversão para fração é feita aqui, em Decimal.
- Datas no formato ISO ("2024-03-15").
- Na saída, dinheiro vem arredondado a centavos e fatores/taxas com 10 casas.
  O arredondamento é só de APRESENTAÇÃO: o cálculo inteiro roda em Decimal
  sem arredondar.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .calculo import indices
from .calculo.liquido import calcular_vf_liquido
from .calculo.modelo import PremissasAtivo, expedicao_do_ano_orcamento, fim_loa
from .calculo.motor import calcular_vf_bruto
from .calculo.perfis import PERFIS, obter_perfil

CEM = Decimal("100")
CENTAVO = Decimal("0.01")
DEZ_CASAS = Decimal("0.0000000001")

# Séries cuja data mais recente a tela mostra como "base atualizada até".
SERIES_STATUS = {
    433: "IPCA",
    7478: "IPCA-15",
    4390: "Selic",
    29543: "Taxa Legal",
}

# Resumo de cada perfil para o usuário final. A `descricao` de perfis.py é
# documentação técnica (cita campos e regimes internos) e fica para a equipe.
RESUMOS = {
    "federal_servidores": (
        "Ações de servidores e empregados públicos na Justiça Federal. Verba "
        "remuneratória: descontos de PSS e IR-RRA."
    ),
    "federal_fazenda_geral": (
        "Ações condenatórias em geral contra a Fazenda Pública federal. Retenção "
        "de IRRF de 3% sobre o montante global."
    ),
    "federal_previdenciario": (
        "Benefícios previdenciários. Correção pelo índice próprio do "
        "previdenciário e retenção de IRRF de 3% sobre o montante global."
    ),
    "federal_desap": (
        "Desapropriações. Permite incluir juros compensatórios quando o principal "
        "informado ainda não os contempla. Retenção de IRRF de 3%."
    ),
    "deposito_judicial": (
        "Montante já depositado em conta judicial, remunerado pela poupança. "
        "Basta informar o montante e a data do depósito."
    ),
    "federal_tributario": (
        "Repetição de indébito tributário. Correção até 01/1996 e, a partir daí, "
        "Selic linear sobre o principal. Retenção de IRRF de 3%."
    ),
}

# Chaves do trace/descontos que são fatores, taxas ou frações (10 casas).
# Todo o resto que for Decimal é tratado como dinheiro (2 casas).
_CHAVES_FATOR = ("fator", "frac", "soma", "taxa", "aliquota", "share")


class ErroEntrada(ValueError):
    """Input inválido — vira HTTP 400 com a mensagem para o usuário."""


# ------------------------------------------------------------------ #
# Leitura do input                                                    #
# ------------------------------------------------------------------ #
def _decimal(payload: dict, campo: str, rotulo: str, padrao: str = "0") -> Decimal:
    bruto = payload.get(campo)
    if bruto in (None, ""):
        bruto = padrao
    try:
        valor = Decimal(str(bruto).strip())
    except InvalidOperation as e:
        raise ErroEntrada(f"{rotulo}: valor inválido ({bruto!r}).") from e
    if not valor.is_finite():
        raise ErroEntrada(f"{rotulo}: valor inválido ({bruto!r}).")
    return valor


def _pct(payload: dict, campo: str, rotulo: str, padrao: str = "0") -> Decimal:
    return _decimal(payload, campo, rotulo, padrao) / CEM


def _data(payload: dict, campo: str, rotulo: str, obrigatoria: bool = False):
    bruto = payload.get(campo)
    if bruto in (None, ""):
        if obrigatoria:
            raise ErroEntrada(f"{rotulo} é obrigatória.")
        return None
    try:
        return date.fromisoformat(str(bruto))
    except ValueError as e:
        raise ErroEntrada(f"{rotulo}: data inválida ({bruto!r}).") from e


def _inteiro(payload: dict, campo: str, rotulo: str) -> int:
    bruto = payload.get(campo)
    if bruto in (None, ""):
        return 0
    try:
        return int(str(bruto).strip())
    except ValueError as e:
        raise ErroEntrada(f"{rotulo}: número inteiro inválido ({bruto!r}).") from e


def montar_premissas(payload: dict) -> PremissasAtivo:
    """Converte o corpo JSON da requisição em `PremissasAtivo`."""
    chave = payload.get("perfil") or ""
    if chave not in PERFIS:
        raise ErroEntrada("Selecione o tipo de ativo.")
    perfil = PERFIS[chave]

    comuns = dict(
        percentual_ativo=_pct(payload, "percentual_ativo_pct", "Percentual do ativo", "100"),
        honorarios_percentual=_pct(payload, "honorarios_pct", "Honorários"),
        destacamento_honorarios=bool(payload.get("destacamento_honorarios", True)),
        aliquota_imposto=_pct(payload, "aliquota_imposto_pct", "Alíquota de IRRF"),
    )

    if perfil.get("motor") == "deposito_judicial":
        return PremissasAtivo.deposito_judicial(
            _decimal(payload, "valor_principal", "Montante depositado"),
            _data(payload, "data_deposito", "Data do depósito", obrigatoria=True),
            **comuns,
        )

    depositado = bool(payload.get("precatorio_depositado"))
    # Juros de mora: incidem por padrão; só um False explícito os desliga.
    aplicar_mora = payload.get("aplicar_juros_mora", True) is not False
    return PremissasAtivo(
        perfil=chave,
        valor_principal=_decimal(payload, "valor_principal", "Valor do principal"),
        valor_juros=_decimal(payload, "valor_juros", "Valor dos juros"),
        data_base=_data(payload, "data_base", "Data base", obrigatoria=True),
        ano_orcamento=_inteiro(payload, "ano_orcamento", "Ano orçamentário"),
        pss_percentual=_pct(payload, "pss_pct", "Alíquota de PSS"),
        pss_valor=_decimal(payload, "pss_valor", "Valor do PSS"),
        meses_ir_rra=_inteiro(payload, "meses_ir_rra", "Meses do IR-RRA"),
        ir_rra_base=payload.get("ir_rra_base") or "integral",
        aplicar_juros_mora=aplicar_mora,
        data_base_juros=(
            _data(payload, "data_base_juros", "Data base dos juros") if aplicar_mora else None
        ),
        aplicar_juros_compensatorios=bool(payload.get("aplicar_juros_compensatorios")),
        transito_antes_adi2332=bool(payload.get("transito_antes_adi2332")),
        precatorio_depositado=depositado,
        data_deposito=_data(payload, "data_deposito", "Data do depósito") if depositado else None,
        **comuns,
    )


# ------------------------------------------------------------------ #
# Serialização                                                        #
# ------------------------------------------------------------------ #
# Formato "f": sempre notação fixa — str() de um zero com 10 casas sai "0E-10".
def _dinheiro(v: Decimal) -> str:
    return f"{v.quantize(CENTAVO, rounding=ROUND_HALF_UP):f}"


def _fator(v: Decimal) -> str:
    return f"{v.quantize(DEZ_CASAS, rounding=ROUND_HALF_UP):f}"


def _serializar(obj, chave: str = ""):
    """Converte Decimal/date/sqlite3.Row recursivamente para tipos JSON."""
    if isinstance(obj, Decimal):
        if any(p in chave for p in _CHAVES_FATOR):
            return _fator(obj)
        return _dinheiro(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serializar(v, k) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serializar(v, chave) for v in obj]
    return obj


# ------------------------------------------------------------------ #
# Endpoints                                                           #
# ------------------------------------------------------------------ #
def listar_perfis() -> list[dict]:
    """Perfis disponíveis e o que cada um exige da tela."""
    return [
        {
            "chave": chave,
            "nome": p["nome"],
            "descricao": p["descricao"],
            "resumo": RESUMOS.get(chave, ""),
            "requer_loa": p.get("requer_loa", True),
            "deposito_judicial": p.get("motor") == "deposito_judicial",
            "verba_remuneratoria": bool(p.get("verba_remuneratoria")),
            "juros_compensatorios": bool(p.get("juros_compensatorios")),
            "irrf_automatico": p.get("irrf_automatico", True),
        }
        for chave, p in PERFIS.items()
    ]


def status_base(conn: sqlite3.Connection) -> list[dict]:
    """Mês mais recente de cada série que limita a data final do cálculo."""
    saida = []
    for codigo, nome in SERIES_STATUS.items():
        try:
            ultimo = indices.ultimo_mes_serie(conn, codigo).isoformat()
        except ValueError:
            ultimo = None
        saida.append({"codigo": codigo, "nome": nome, "ultimo_mes": ultimo})
    return saida


def calcular(conn: sqlite3.Connection, payload: dict) -> dict:
    """
    Roda o cálculo completo (bruto + líquido) e devolve o resultado em JSON.

    Levanta ErroEntrada para qualquer problema de input — inclusive os
    `ValueError` da validação do modelo, que já trazem mensagem clara.
    """
    try:
        premissas = montar_premissas(payload)
        ate = _data(payload, "data_calculo", "Data do cálculo")
        bruto = calcular_vf_bruto(conn, premissas, ate=ate)
        liquido = calcular_vf_liquido(conn, bruto, premissas)
    except ErroEntrada:
        raise
    except (ValueError, KeyError) as e:
        raise ErroEntrada(str(e).strip("'\"")) from e

    perfil = obter_perfil(premissas.perfil)
    datas = {"data_base": premissas.data_base}
    if premissas.aplicar_juros_mora and premissas.data_base_juros:
        datas["data_base_juros"] = premissas.data_base_juros
    if perfil.get("requer_loa", True):
        datas["expedicao"] = expedicao_do_ano_orcamento(premissas.ano_orcamento)
        datas["fim_loa"] = fim_loa(premissas.ano_orcamento)
    if premissas.data_deposito:
        datas["deposito"] = premissas.data_deposito

    return _serializar({
        "perfil": {"chave": premissas.perfil, "nome": perfil["nome"]},
        "posicao": {
            "data": bruto.mes_posicao,
            # Índice mensal: o valor vale o mês inteiro (sem pro rata die).
            # Poupança: accrual diário, a posição é o próprio dia.
            "tipo": "mes" if bruto.por_indice_mensal else "dia",
            "ultimo_indice": bruto.data_final,
        },
        "datas": datas,
        "juros_mora": {
            "aplicados": premissas.aplicar_juros_mora,
            "desde": premissas.data_base_juros or premissas.data_base,
        },
        "valores": {
            "valor_atualizado": liquido.valor_atualizado,
            "vf_bruto": liquido.vf_bruto,
            # Destacados: honorários saem antes dos impostos, que incidem sobre
            # a base_impostos. Não destacados: impostos primeiro.
            "honorarios_destacados": liquido.honorarios_destacados,
            "base_impostos": liquido.base_impostos,
            "principal": liquido.principal,
            "juros": liquido.juros,
            "selic": liquido.selic,
            "pss": liquido.pss,
            "ir_rra": liquido.ir_rra,
            "irrf_global": liquido.irrf_global,
            "impostos": liquido.impostos,
            "vf_liquido": liquido.vf_liquido,
            "honorarios": liquido.honorarios,
            "total_cedivel": liquido.total_cedivel,
            "percentual_ativo": premissas.percentual_ativo * CEM,
        },
        "descontos": liquido.descontos,
        "memoria": bruto.trace,
    })
