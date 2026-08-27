"""
Cliente e parser da tabela de feriados bancários da ANBIMA.

Necessária para a contagem de dias úteis (base 252) usada no cálculo
pro-rata da poupança entre o último aniversário e a data de cálculo.

Fluxo:
1. Baixa o .xls oficial da ANBIMA.
2. Calcula o SHA-256 e arquiva o arquivo em `referencias/feriados/`.
3. Parseia a aba "Feriados" (colunas: Data | Dia da Semana | Feriado).

Sobre o formato: diferente do "XLS" do CJF (que é HTML disfarçado), este é
um .xls BIFF8 de verdade (documento OLE2) — por isso a dependência `xlrd`,
que lê exatamente esse formato. As datas vêm como serial do Excel
(`ctype == XL_CELL_DATE`), o que permite distinguir com segurança as linhas
de dados das notas de rodapé em texto no fim da planilha.

A própria planilha registra o critério adotado: são "os feriados em que não
há sensibilização das Reservas Bancárias" — não inclui feriados municipais,
eleições nem o último dia útil do ano.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger(__name__)


@dataclass
class Feriado:
    data: date
    dia_semana: str
    descricao: str


@dataclass
class ResultadoFeriados:
    sha256: str
    caminho_arquivo: Path
    feriados: list[Feriado]
    mudou: bool


def _baixar() -> bytes:
    logger.info("FERIADOS: baixando %s", config.FERIADOS_URL)
    req = urllib.request.Request(
        config.FERIADOS_URL, headers={"User-Agent": "calculadora-claims/1.0"}
    )
    with urllib.request.urlopen(req, timeout=config.TIMEOUT_SEGUNDOS) as r:
        return r.read()


def _arquivar(conteudo: bytes, sha256: str) -> Path:
    config.FERIADOS_REFERENCIAS_DIR.mkdir(parents=True, exist_ok=True)
    nome = f"feriados_anbima_{datetime.now():%Y-%m-%d}_{sha256[:8]}.xls"
    caminho = config.FERIADOS_REFERENCIAS_DIR / nome
    caminho.write_bytes(conteudo)
    logger.info("FERIADOS: arquivo salvo em %s", caminho)
    return caminho


def parsear_xls(caminho: Path) -> list[Feriado]:
    """
    Lê a planilha e devolve a lista de feriados.

    Considera linha de dados apenas aquelas cuja primeira célula é uma DATA
    do Excel — assim as notas de rodapé (texto) são naturalmente ignoradas.
    """
    try:
        import xlrd
    except ImportError as e:
        raise RuntimeError(
            "xlrd não está instalado. Rode: pip install xlrd"
        ) from e

    wb = xlrd.open_workbook(str(caminho))
    sh = wb.sheet_by_index(0)

    feriados: list[Feriado] = []
    for r in range(sh.nrows):
        celula = sh.cell(r, 0)
        if celula.ctype != xlrd.XL_CELL_DATE:
            continue
        ano, mes, dia, *_ = xlrd.xldate_as_tuple(celula.value, wb.datemode)
        dia_semana = str(sh.cell_value(r, 1)).strip() if sh.ncols > 1 else ""
        descricao = str(sh.cell_value(r, 2)).strip() if sh.ncols > 2 else ""
        feriados.append(Feriado(date(ano, mes, dia), dia_semana, descricao))

    if not feriados:
        raise ValueError(
            f"Nenhum feriado encontrado em {caminho}. O formato da planilha "
            f"da ANBIMA provavelmente mudou."
        )

    feriados.sort(key=lambda f: f.data)
    logger.info(
        "FERIADOS: %d feriados extraídos (%s → %s).",
        len(feriados), feriados[0].data, feriados[-1].data,
    )
    return feriados


def coletar(sha256_anterior: Optional[str] = None) -> ResultadoFeriados:
    """
    Pipeline: download → hash → comparar → (arquivar + parsear).

    Se o hash for igual ao anterior, retorna `mudou=False` sem reprocessar.
    """
    conteudo = _baixar()
    sha256_atual = hashlib.sha256(conteudo).hexdigest()

    if sha256_anterior == sha256_atual:
        logger.info("FERIADOS: hash inalterado (%s).", sha256_atual[:8])
        return ResultadoFeriados(sha256_atual, Path(""), [], False)

    caminho = _arquivar(conteudo, sha256_atual)
    return ResultadoFeriados(sha256_atual, caminho, parsear_xls(caminho), True)
