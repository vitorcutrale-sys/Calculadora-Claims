"""
Cliente e parser da Tabela Progressiva Mensal do IRPF (Receita Federal).

Coleta ANUAL: a Receita publica uma página por ano-calendário com as tabelas
de incidência e deduções. Coletamos apenas a **Tabela de Incidência Mensal**
— é a que o IR-RRA usa (tabela do mês do recebimento × número de meses).

Fluxo:
1. Baixa a página do ano (tentando os padrões de URL de config).
2. Calcula o SHA-256 e arquiva o HTML em `referencias/irpf/`.
3. Extrai TODAS as tabelas mensais da página, cada uma com sua vigência
   (uma página pode ter mais de uma — ex.: 2025 traz "a partir de maio" e
   "de janeiro a abril").

Formato da página (não usa <table>; é texto em blocos):

    A partir de maio de 2025          <- vigência
    Base de cálculo
    Alíquota
    Dedução
    Até R$ 2.428,80        /  -      /  -
    De R$ 2.428,81 até R$ 2.826,65  /  7,5%  /  R$ 182,16
    ... (5 faixas no total)

Validação estrita: exigimos exatamente 5 faixas, alíquotas
(0 / 7,5 / 15 / 22,5 / 27,5) e limites crescentes. Se a página mudar de
forma, a coleta falha com erro claro — melhor que gravar número errado.

Usa apenas a stdlib.
"""

from __future__ import annotations

import calendar
import hashlib
import logging
import re
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger(__name__)

MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}


# ------------------------------------------------------------------ #
# Tipos                                                               #
# ------------------------------------------------------------------ #
@dataclass
class Faixa:
    """Uma faixa da tabela progressiva mensal."""
    ordem: int                    # 1..5
    base_de: float                # limite inferior (R$)
    base_ate: Optional[float]     # limite superior (None na última faixa)
    aliquota: float               # fração (0.075 para 7,5%)
    deducao: float                # parcela a deduzir (R$)


@dataclass
class TabelaIRPF:
    """Tabela mensal com sua vigência."""
    vigencia_inicio: date
    vigencia_fim: Optional[date]  # None = vigente por prazo indeterminado
    faixas: list[Faixa]


@dataclass
class ResultadoIRPF:
    ano_pagina: int
    url: str
    sha256: str
    caminho_arquivo: Path
    tabelas: list[TabelaIRPF]
    mudou: bool


# ------------------------------------------------------------------ #
# Download / arquivamento                                             #
# ------------------------------------------------------------------ #
def _baixar_pagina(ano: int) -> tuple[str, bytes]:
    """
    Baixa a página do ano tentando os padrões de URL configurados.
    Retorna (url_usada, conteudo). Levanta RuntimeError se nenhuma servir.
    """
    erros = []
    for padrao in config.IRPF_URL_PADROES:
        url = padrao.format(base=config.IRPF_BASE_URL, ano=ano)
        logger.info("IRPF: tentando %s", url)
        req = urllib.request.Request(
            url, headers={"User-Agent": "calculadora-claims/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=config.TIMEOUT_SEGUNDOS) as r:
                return url, r.read()
        except urllib.error.HTTPError as e:
            erros.append(f"{url} -> HTTP {e.code}")
            logger.warning("IRPF: %s respondeu HTTP %s.", url, e.code)
    raise RuntimeError(
        f"IRPF: nenhuma URL disponível para o ano {ano}. Tentativas: "
        + "; ".join(erros)
    )


def _salvar_html(conteudo: bytes, ano: int, sha256: str) -> Path:
    """Arquiva o HTML em referencias/irpf/ com data e hash no nome."""
    config.IRPF_REFERENCIAS_DIR.mkdir(parents=True, exist_ok=True)
    nome = f"IRPF_{ano}_{datetime.now():%Y-%m-%d}_{sha256[:8]}.html"
    caminho = config.IRPF_REFERENCIAS_DIR / nome
    caminho.write_bytes(conteudo)
    logger.info("IRPF: página arquivada em %s", caminho)
    return caminho


# ------------------------------------------------------------------ #
# Parser                                                              #
# ------------------------------------------------------------------ #
def _sem_acento(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    ).lower()


def _linhas(html: str) -> list[str]:
    """Converte o HTML em linhas de texto limpas, preservando a ordem."""
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    h = re.sub(r"(?i)</(p|div|tr|td|th|li|h[1-6]|table)>", "\n", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = h.replace("&nbsp;", " ").replace("\xa0", " ").replace("&amp;", "&")
    h = re.sub(r"[ \t]+", " ", h)
    return [l.strip() for l in h.splitlines() if l.strip()]


_RE_VALOR = re.compile(r"R\$\s*([\d.]+,\d{2})")
_RE_PCT = re.compile(r"^([\d,]+)\s*%$")


def _valor(s: str) -> Optional[float]:
    m = _RE_VALOR.search(s)
    if not m:
        return None
    return float(m.group(1).replace(".", "").replace(",", "."))


def _vigencia(linha: str) -> Optional[tuple[date, Optional[date]]]:
    """
    Interpreta a linha de vigência.

    "A partir de maio de 2025"             -> (2025-05-01, None)
    "De janeiro a abril de 2025"           -> (2025-01-01, 2025-04-30)
    "De maio de 2023 a janeiro de 2024"    -> (2023-05-01, 2024-01-31)
    """
    t = _sem_acento(linha)

    # Intervalo que cruza anos: "de <mês> de <ano> a <mês> de <ano>"
    m = re.search(r"de\s+(\w+)\s+de\s+(\d{4})\s+a\s+(\w+)\s+de\s+(\d{4})", t)
    if m and m.group(1) in MESES and m.group(3) in MESES:
        ano_ini, ano_fim = int(m.group(2)), int(m.group(4))
        mes_ini, mes_fim = MESES[m.group(1)], MESES[m.group(3)]
        ultimo = calendar.monthrange(ano_fim, mes_fim)[1]
        return date(ano_ini, mes_ini, 1), date(ano_fim, mes_fim, ultimo)

    # Intervalo no mesmo ano: "de <mês> a <mês> de <ano>"
    m = re.search(r"de\s+(\w+)\s+a\s+(\w+)\s+de\s+(\d{4})", t)
    if m and m.group(1) in MESES and m.group(2) in MESES:
        ano = int(m.group(3))
        mes_ini, mes_fim = MESES[m.group(1)], MESES[m.group(2)]
        ultimo = calendar.monthrange(ano, mes_fim)[1]
        return date(ano, mes_ini, 1), date(ano, mes_fim, ultimo)

    m = re.search(r"a partir\s+(?:de\s+)?(\w+)\s+de\s+(\d{4})", t)
    if m and m.group(1) in MESES:
        return date(int(m.group(2)), MESES[m.group(1)], 1), None

    return None


def _parsear_bloco(linhas: list[str], i: int) -> Optional[list[Faixa]]:
    """
    Tenta ler 5 faixas a partir da linha `i` (primeira linha de dados,
    logo após os cabeçalhos). Cada faixa ocupa 3 linhas: base / alíquota /
    dedução. Retorna None se a forma não bater.
    """
    faixas: list[Faixa] = []
    pos = i
    for ordem in range(1, config.IRPF_QTD_FAIXAS + 1):
        if pos + 2 >= len(linhas):
            return None
        l_base, l_aliq, l_ded = linhas[pos], linhas[pos + 1], linhas[pos + 2]
        t_base = _sem_acento(l_base)

        if ordem == 1:
            # "Até R$ 2.428,80" -> faixa isenta
            if not t_base.startswith("ate"):
                return None
            ate = _valor(l_base)
            if ate is None:
                return None
            faixas.append(Faixa(1, 0.0, ate, 0.0, 0.0))
        else:
            aliq_m = _RE_PCT.match(l_aliq.strip())
            if not aliq_m:
                return None
            aliquota = float(aliq_m.group(1).replace(",", "."))
            deducao = _valor(l_ded)
            if deducao is None:
                return None

            if t_base.startswith("acima"):
                de = _valor(l_base)
                if de is None:
                    return None
                faixas.append(Faixa(ordem, de, None, aliquota, deducao))
            else:
                # "De R$ 2.428,81 até R$ 2.826,65"
                vals = _RE_VALOR.findall(l_base)
                if len(vals) != 2:
                    return None
                de = float(vals[0].replace(".", "").replace(",", "."))
                ate = float(vals[1].replace(".", "").replace(",", "."))
                faixas.append(Faixa(ordem, de, ate, aliquota, deducao))
        pos += 3
    return faixas


def _validar(faixas: list[Faixa], contexto: str) -> None:
    """Validação estrita da forma da tabela. Levanta ValueError."""
    if len(faixas) != config.IRPF_QTD_FAIXAS:
        raise ValueError(
            f"IRPF {contexto}: esperava {config.IRPF_QTD_FAIXAS} faixas, "
            f"achei {len(faixas)}."
        )
    aliquotas = tuple(f.aliquota for f in faixas)
    if aliquotas != config.IRPF_ALIQUOTAS_ESPERADAS:
        raise ValueError(
            f"IRPF {contexto}: alíquotas {aliquotas} diferem das esperadas "
            f"{config.IRPF_ALIQUOTAS_ESPERADAS}. A página pode ter mudado de "
            f"formato ou a lei mudou — confira antes de gravar."
        )
    limites = [f.base_de for f in faixas]
    if limites != sorted(limites) or len(set(limites)) != len(limites):
        raise ValueError(f"IRPF {contexto}: limites não são crescentes: {limites}.")


def parsear_html(html: str, ano: int) -> list[TabelaIRPF]:
    """
    Extrai todas as Tabelas de Incidência Mensal da página, cada uma com
    sua vigência. Ignora a tabela anual (identificada pela magnitude) e as
    demais (redução, capital, PLR — que não têm o cabeçalho completo).
    """
    linhas = _linhas(html)
    tabelas: list[TabelaIRPF] = []

    for i, linha in enumerate(linhas):
        # Âncora: cabeçalho "Base de cálculo" / "Alíquota" / "Dedução"
        if _sem_acento(linha) != "base de calculo":
            continue
        if i + 2 >= len(linhas):
            continue
        if _sem_acento(linhas[i + 1]) != "aliquota":
            continue
        if not _sem_acento(linhas[i + 2]).startswith("deducao"):
            continue

        faixas = _parsear_bloco(linhas, i + 3)
        if not faixas:
            continue

        # Distingue mensal de anual pela magnitude do 1º limite.
        if (faixas[0].base_ate or 0) > config.IRPF_LIMITE_MAX_MENSAL:
            logger.debug("IRPF: bloco na linha %d parece ANUAL; ignorado.", i)
            continue

        # Vigência: procura para trás a linha que a descreve.
        vig = None
        for j in range(i - 1, max(i - 6, -1), -1):
            vig = _vigencia(linhas[j])
            if vig:
                break
        if not vig:
            logger.warning(
                "IRPF: tabela mensal na linha %d sem vigência identificável; "
                "ignorada.", i,
            )
            continue

        _validar(faixas, f"{ano} (vigência {vig[0]})")
        tabelas.append(TabelaIRPF(vig[0], vig[1], faixas))

    if not tabelas:
        raise ValueError(
            f"IRPF {ano}: nenhuma Tabela de Incidência Mensal encontrada na "
            f"página. O formato provavelmente mudou — verifique o HTML "
            f"arquivado em referencias/irpf/."
        )

    tabelas.sort(key=lambda t: t.vigencia_inicio)
    logger.info(
        "IRPF %d: %d tabela(s) mensal(is) extraída(s): %s",
        ano, len(tabelas),
        ", ".join(f"{t.vigencia_inicio:%Y-%m}" for t in tabelas),
    )
    return tabelas


# ------------------------------------------------------------------ #
# Função principal                                                    #
# ------------------------------------------------------------------ #
def coletar_ano(ano: int, sha256_anterior: Optional[str] = None) -> ResultadoIRPF:
    """
    Pipeline de um ano: download → hash → comparar → (arquivar + parsear).

    Se o hash for igual ao anterior, retorna `mudou=False` sem reprocessar.
    """
    url, conteudo = _baixar_pagina(ano)
    sha256_atual = hashlib.sha256(conteudo).hexdigest()

    if sha256_anterior == sha256_atual:
        logger.info("IRPF %d: hash inalterado (%s).", ano, sha256_atual[:8])
        return ResultadoIRPF(ano, url, sha256_atual, Path(""), [], False)

    caminho = _salvar_html(conteudo, ano, sha256_atual)
    html = conteudo.decode("utf-8", errors="replace")
    tabelas = parsear_html(html, ano)
    return ResultadoIRPF(ano, url, sha256_atual, caminho, tabelas, True)
