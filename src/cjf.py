"""
Cliente e parser das Tabelas de Correção Monetária do CJF (Conselho da Justiça Federal).

Referência: https://sicom.cjf.jus.br/tabelaCorMor.php

Fluxo:
1. Faz POST ao formulário SICOM para obter a URL do arquivo gerado.
2. Baixa o XLS (HTML disfarçado de Excel) do CJF.
3. Calcula SHA-256 do conteúdo e compara com a última coleta:
   - igual  → sem mudança, retorna sem reprocessar.
   - novo   → salva o arquivo em `referencias/cjf/` e parseia os coeficientes.

Parâmetros do formulário:
    acao           : sempre "geraTabelaIndice"
    tipoTabela     : "TCM" (Tabela de Correção Monetária) | "VHI" (Valores Históricos dos Indexadores)
    seqEncadeamento: 1–6 (tipo de ação judicial)
    mesIndice      : mês de referência (ex.: "05")
    anoIndice      : ano de referência (ex.: "2025")
    tipoArquivo    : "xls"

O arquivo XLS é na verdade um HTML com namespace do Excel (MHTML). Os dados estão
em tabela HTML com linhas no formato: [ano, jan, fev, ..., dez], valores em decimal
brasileiro (vírgula como separador decimal, ponto como milhar).
"""

from __future__ import annotations

import hashlib
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Tipos                                                               #
# ------------------------------------------------------------------ #
@dataclass
class ResultadoCJF:
    """Resultado da coleta de uma tabela CJF."""
    tabela_chave: str          # ex.: "TCM_3", "VHI_2"
    tipo_tabela: str           # "TCM" ou "VHI"
    seq_encadeamento: int      # 1..6
    mes_referencia: int        # 1..12
    ano_referencia: int
    sha256: str
    caminho_arquivo: Path
    fatores: list[tuple[int, int, float]]  # (ano, mes, valor)
    mudou: bool


# ------------------------------------------------------------------ #
# Download                                                            #
# ------------------------------------------------------------------ #
_BASE_URL = "https://sicom.cjf.jus.br"
_FORM_URL = f"{_BASE_URL}/tabelaCorMor.php"
_RE_REDIRECT = re.compile(r"window\.location\.href='([^']+)'")

_NOME_ARQUIVO = {
    "TCM": "tabela_de_correcao_monetaria",
    "VHI": "valores_historicos_dos_indexadores",
}


def _post_formulario(
    tipo_tabela: str,
    seq_encadeamento: int,
    mes: int,
    ano: int,
) -> bytes:
    """
    Faz POST ao formulário SICOM e baixa o XLS gerado.

    Estratégia de dois passos:
    1. POST retorna um HTML com window.location.href para o arquivo temporário.
    2. Baixamos esse arquivo diretamente.
    """
    dados = urllib.parse.urlencode({
        "acao": "geraTabelaIndice",
        "tipoTabela": tipo_tabela,
        "seqEncadeamento": str(seq_encadeamento),
        "mesIndice": f"{mes:02d}",
        "anoIndice": str(ano),
        "tipoArquivo": "xls",
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "calculadora-claims/1.0",
        "Referer": _FORM_URL,
    }

    req = urllib.request.Request(_FORM_URL, data=dados, headers=headers)
    with urllib.request.urlopen(req, timeout=config.TIMEOUT_SEGUNDOS) as resp:
        html_resposta = resp.read().decode("latin-1", errors="replace")

    m = _RE_REDIRECT.search(html_resposta)
    if not m:
        # URL direta por convenção: {ano}_{mes:02d}_{nome_base}.xls
        caminho_relativo = (
            f"arquivos/temp/{ano}_{mes:02d}_{_NOME_ARQUIVO[tipo_tabela]}.xls"
        )
        logger.warning(
            "CJF: redirect não encontrado na resposta; tentando URL padrão: %s",
            caminho_relativo,
        )
    else:
        caminho_relativo = m.group(1)

    url_arquivo = f"{_BASE_URL}/{caminho_relativo}"
    logger.info("CJF: baixando arquivo de %s", url_arquivo)

    req2 = urllib.request.Request(
        url_arquivo, headers={"User-Agent": "calculadora-claims/1.0"}
    )
    with urllib.request.urlopen(req2, timeout=config.TIMEOUT_SEGUNDOS) as resp2:
        return resp2.read()


def _calcular_sha256(conteudo: bytes) -> str:
    return hashlib.sha256(conteudo).hexdigest()


def _salvar_arquivo(
    conteudo: bytes,
    tabela_chave: str,
    sha256: str,
) -> Path:
    """Salva XLS em referencias/cjf/ com nome {chave}_AAAA-MM-DD_{sha256[:8]}.xls"""
    config.CJF_REFERENCIAS_DIR.mkdir(parents=True, exist_ok=True)
    data = datetime.now().strftime("%Y-%m-%d")
    nome = f"{tabela_chave}_{data}_{sha256[:8]}.xls"
    caminho = config.CJF_REFERENCIAS_DIR / nome
    caminho.write_bytes(conteudo)
    logger.info("CJF: arquivo salvo em %s", caminho)
    return caminho


# ------------------------------------------------------------------ #
# Parser                                                              #
# ------------------------------------------------------------------ #
_RE_ANO_CELULA = re.compile(r"^\s*(\d{4})\s*$")
_RE_NUM_BR = re.compile(r"\d{1,3}(?:\.\d{3})*,\d+")


def _br_para_float(s: str) -> float:
    """'1.045,540000' → 1045.54  |  '0,0075589248' → 0.0075589248"""
    return float(s.replace(".", "").replace(",", "."))


def _extrair_texto_celulas(html: str) -> list[str]:
    """Extrai texto limpo de todas as células <td>...</td>."""
    linhas_texto = []
    for row_m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        row_html = row_m.group(1)
        celulas = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL)
        celulas_txt = [re.sub(r"<[^>]+>", "", c).strip() for c in celulas]
        celulas_txt = [c for c in celulas_txt if c]
        if celulas_txt:
            linhas_texto.append(celulas_txt)
    return linhas_texto


def parsear_xls(caminho: Path) -> list[tuple[int, int, float]]:
    """
    Lê o XLS (HTML) do CJF e devolve lista de (ano, mes, valor).

    Ignora seções de cabeçalho, notas de rodapé e células vazias.
    Trata múltiplas seções dentro do mesmo arquivo (ex.: VHI com ORTN, OTN...).
    """
    conteudo = caminho.read_bytes().decode("latin-1", errors="replace")
    linhas = _extrair_texto_celulas(conteudo)

    fatores: list[tuple[int, int, float]] = []
    anos_vistos: set[int] = set()

    for celulas in linhas:
        if not celulas:
            continue

        # Linha de dados: primeira célula é exatamente um ano de 4 dígitos
        m = _RE_ANO_CELULA.match(celulas[0])
        if not m:
            continue
        ano = int(m.group(1))
        if not (1900 <= ano <= 2200):
            continue
        if ano in anos_vistos:
            continue
        anos_vistos.add(ano)

        # Células 1..12: valores mensais (jan..dez)
        valores: list[Optional[float]] = []
        for cell in celulas[1:13]:
            cell = cell.strip()
            if not cell or cell in ("::::::::::", "-"):
                valores.append(None)
            else:
                nums = _RE_NUM_BR.findall(cell)
                if nums:
                    valores.append(_br_para_float(nums[0]))
                else:
                    valores.append(None)

        for mes_idx, valor in enumerate(valores, start=1):
            if valor is not None:
                fatores.append((ano, mes_idx, valor))

    fatores.sort(key=lambda x: (x[0], x[1]))
    logger.info(
        "CJF: %d fatores extraídos (anos %s..%s).",
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
    tipo_tabela: str,
    seq_encadeamento: int,
    mes_referencia: int,
    ano_referencia: int,
    sha256_anterior: Optional[str] = None,
) -> ResultadoCJF:
    """
    Pipeline completo de uma combinação CJF:
    POST → redirect → download → hash → comparar → (salvar + parsear se mudou).
    """
    conteudo = _post_formulario(tipo_tabela, seq_encadeamento, mes_referencia, ano_referencia)
    sha256_atual = _calcular_sha256(conteudo)

    if sha256_anterior == sha256_atual:
        logger.info(
            "CJF %s: hash inalterado (%s). Nada a fazer.",
            tabela_chave, sha256_atual[:8],
        )
        return ResultadoCJF(
            tabela_chave=tabela_chave,
            tipo_tabela=tipo_tabela,
            seq_encadeamento=seq_encadeamento,
            mes_referencia=mes_referencia,
            ano_referencia=ano_referencia,
            sha256=sha256_atual,
            caminho_arquivo=Path(""),
            fatores=[],
            mudou=False,
        )

    caminho = _salvar_arquivo(conteudo, tabela_chave, sha256_atual)
    fatores = parsear_xls(caminho)
    logger.info(
        "CJF %s: nova versão (%s), %d fatores.",
        tabela_chave, sha256_atual[:8], len(fatores),
    )
    return ResultadoCJF(
        tabela_chave=tabela_chave,
        tipo_tabela=tipo_tabela,
        seq_encadeamento=seq_encadeamento,
        mes_referencia=mes_referencia,
        ano_referencia=ano_referencia,
        sha256=sha256_atual,
        caminho_arquivo=caminho,
        fatores=fatores,
        mudou=True,
    )
