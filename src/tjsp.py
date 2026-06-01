"""
Cliente e parser das Tabelas Práticas do TJSP.

Fluxo:
1. Baixa o PDF do endpoint TJSP usando o `codigo` do arquivo no CMS.
2. Calcula o SHA-256 do conteúdo.
3. Compara com o hash da última publicação salva:
   - igual  → não há mudança, retorna sem reprocessar
   - novo   → salva o PDF em `referencias/tjsp/` com timestamp e hash
              curto no nome, faz parsing e devolve a lista de fatores.

O parser lida com:
- Layout em matriz (linhas = anos, colunas = 12 meses).
- Decimal brasileiro (`1.045,540000` = 1045.54).
- Células vazias marcadas com `::::::::::` ou em branco (meses
  anteriores ao início da série ou ainda não publicados).
- Notas de rodapé sobre conversões monetárias (Cr$, Cz$, etc.) — são
  apenas ignoradas; quem aplica os fatores no cálculo é que precisa
  considerar as conversões. As notas ficam preservadas no PDF salvo.

Depende de `pdfplumber` (única biblioteca externa do projeto até aqui).
"""

from __future__ import annotations

import hashlib
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Tipos                                                               #
# ------------------------------------------------------------------ #
@dataclass
class ResultadoTJSP:
    """Resultado da coleta de uma tabela TJSP."""
    tabela_chave: str
    codigo_arquivo: int
    sha256: str
    caminho_arquivo: Path
    fatores: list[tuple[int, int, float]]  # (ano, mes, fator)
    mudou: bool                            # False se hash igual ao anterior

# ------------------------------------------------------------------ #
# Download                                                           #
# ------------------------------------------------------------------ #
def _baixar_pdf(codigo_arquivo: int) -> bytes:
    """Baixa o PDF do endpoint TJSP. Retorna os bytes brutos."""
    query = urllib.parse.urlencode({"codigo": codigo_arquivo})
    url = f"{config.TJSP_BASE_URL}?{query}"

    logger.info("TJSP: baixando arquivo %d de %s", codigo_arquivo, url)

    req = urllib.request.Request(
        url, headers={"User-Agent": "calculadora-claims/1.0"}
    )
    with urllib.request.urlopen(req, timeout=config.TIMEOUT_SEGUNDOS) as resp:
        return resp.read()


def _calcular_sha256(conteudo: bytes) -> str:
    return hashlib.sha256(conteudo).hexdigest()


def _salvar_pdf(
    conteudo: bytes,
    tabela_chave: str,
    sha256: str,
) -> Path:
    """
    Salva o PDF em referencias/tjsp/ com nome:
        {tabela_chave}_AAAA-MM-DD_{sha256[:8]}.pdf
    """
    config.TJSP_REFERENCIAS_DIR.mkdir(parents=True, exist_ok=True)
    data = datetime.now().strftime("%Y-%m-%d")
    nome = f"{tabela_chave}_{data}_{sha256[:8]}.pdf"
    caminho = config.TJSP_REFERENCIAS_DIR / nome
    caminho.write_bytes(conteudo)
    logger.info("TJSP: PDF salvo em %s", caminho)
    return caminho


# ------------------------------------------------------------------ #
# Parser                                                              #
# ------------------------------------------------------------------ #
# Regex de número no formato brasileiro:
#   1.045,540000   |   66,205504   |   10.000,000000
_RE_NUMERO_BR = re.compile(r"\d{1,3}(?:\.\d{3})*,\d+")

# Marcador de célula vazia no PDF do TJSP
_MARCADOR_VAZIO = "::::::::::"

# Linha de dados começa com um ano de 4 dígitos (1800-2199)
_RE_ANO_INICIO = re.compile(r"^\s*(\d{4})\s")


def _br_para_float(s: str) -> float:
    """'1.045,540000' → 1045.54  |  '66,205504' → 66.205504"""
    return float(s.replace(".", "").replace(",", "."))


def _parsear_linha_de_ano(linha: str) -> Optional[tuple[int, list[Optional[float]]]]:
    """
    Tenta parsear uma linha que represente um ano da tabela TJSP.

    Retorna (ano, [12 valores ou None]) ou None se não for linha de dados.

    Estratégia:
    - A linha deve começar com 4 dígitos (o ano).
    - Pegamos todos os tokens "número BR" e marcadores vazios na ordem
      em que aparecem.
    - O TJSP repete o ano no início e no fim da linha; descartamos
      possíveis ocorrências do ano que apareçam como números soltos.
    - Os primeiros 12 tokens válidos = janeiro a dezembro.
    """
    match = _RE_ANO_INICIO.match(linha)
    if not match:
        return None
    ano = int(match.group(1))
    if not (1900 <= ano <= 2200):
        return None

    # Remove o ano do início e o ano que costuma aparecer no fim.
    # Aproximação simples: substitui ocorrências do ano por espaço.
    nucleo = re.sub(rf"\b{ano}\b", " ", linha)

    # Extrai tokens: ou números BR ou o marcador vazio.
    tokens: list[Optional[float]] = []
    pos = 0
    while pos < len(nucleo) and len(tokens) < 12:
        # Marcador vazio?
        if nucleo[pos:pos + len(_MARCADOR_VAZIO)] == _MARCADOR_VAZIO:
            tokens.append(None)
            pos += len(_MARCADOR_VAZIO)
            continue
        # Número BR?
        m = _RE_NUMERO_BR.match(nucleo, pos)
        if m:
            tokens.append(_br_para_float(m.group(0)))
            pos = m.end()
            continue
        pos += 1

    if not tokens:
        return None
    # Garante 12 posições (preenche com None se faltar — ex.: meses futuros)
    while len(tokens) < 12:
        tokens.append(None)
    return ano, tokens


def parsear_pdf(caminho: Path) -> list[tuple[int, int, float]]:
    """
    Lê o PDF da tabela e devolve lista de tuplas (ano, mes, fator),
    com mes em 1..12. Células vazias são omitidas.
    """
    # Import local: pdfplumber pode não estar instalado quando alguém
    # roda apenas a parte BACEN. Falhamos com mensagem clara aqui.
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError(
            "pdfplumber não está instalado. Rode: pip install pdfplumber"
        ) from e

    fatores: list[tuple[int, int, float]] = []
    anos_vistos: set[int] = set()

    with pdfplumber.open(str(caminho)) as pdf:
        for pagina in pdf.pages:
            texto = pagina.extract_text() or ""
            for linha in texto.splitlines():
                resultado = _parsear_linha_de_ano(linha)
                if resultado is None:
                    continue
                ano, valores = resultado
                # Evita duplicidade se a mesma linha aparecer fragmentada
                if ano in anos_vistos:
                    continue
                anos_vistos.add(ano)
                for mes_idx, valor in enumerate(valores, start=1):
                    if valor is None:
                        continue
                    fatores.append((ano, mes_idx, valor))

    fatores.sort(key=lambda x: (x[0], x[1]))
    logger.info(
        "TJSP: %d fatores extraídos (anos %s..%s).",
        len(fatores),
        min(anos_vistos) if anos_vistos else "-",
        max(anos_vistos) if anos_vistos else "-",
    )
    return fatores


# ------------------------------------------------------------------ #
# Função principal                                                    #
# ------------------------------------------------------------------ #
def coletar_tabela(
    tabela_chave: str,
    codigo_arquivo: int,
    sha256_anterior: Optional[str] = None,
) -> ResultadoTJSP:
    """
    Pipeline completo de uma tabela TJSP:
    download → hash → comparar → (salvar + parsear se mudou).

    Se o hash for igual ao anterior, retorna ResultadoTJSP com
    `mudou=False` e `fatores=[]` (não reprocessa o PDF).
    """
    conteudo = _baixar_pdf(codigo_arquivo)
    sha256_atual = _calcular_sha256(conteudo)

    if sha256_anterior == sha256_atual:
        logger.info(
            "TJSP %s: hash inalterado (%s). Nada a fazer.",
            tabela_chave, sha256_atual[:8],
        )
        return ResultadoTJSP(
            tabela_chave=tabela_chave,
            codigo_arquivo=codigo_arquivo,
            sha256=sha256_atual,
            caminho_arquivo=Path(""),  # não foi salvo
            fatores=[],
            mudou=False,
        )

    caminho = _salvar_pdf(conteudo, tabela_chave, sha256_atual)
    fatores = parsear_pdf(caminho)
    logger.info(
        "TJSP %s: nova versão (%s), %d fatores.",
        tabela_chave, sha256_atual[:8], len(fatores),
    )
    return ResultadoTJSP(
        tabela_chave=tabela_chave,
        codigo_arquivo=codigo_arquivo,
        sha256=sha256_atual,
        caminho_arquivo=caminho,
        fatores=fatores,
        mudou=True,
    )
