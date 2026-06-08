"""
Importador das Tabelas de Juros de Mora (condenações da Fazenda Pública).

Diferente de bacen/tjsp/cjf, este módulo NÃO baixa nada da internet:
os juros de mora são dados HISTÓRICOS e fechados (deixaram de ser aplicados
em 09/12/2021, art. 3º da EC 113/2021). A importação é feita uma única vez
a partir de PDFs locais informados pelo usuário.

Fluxo de cada tabela:
1. Lê o PDF de origem (pasta informada em --origem).
2. Calcula o SHA-256 do conteúdo.
3. Compara com o hash da última publicação salva:
   - igual → nada a fazer (idempotente)
   - novo  → arquiva uma cópia em referencias/juros/ com timestamp e hash
             no nome, faz o parsing e devolve as taxas.

Parser:
- Usa pdfplumber.extract_tables(), que PRESERVA as colunas (mês), em vez
  de regex sobre texto puro. Isso é essencial porque o ano inicial (1964)
  é parcial — só tem out/nov/dez — e o alinhamento por coluna garante que
  esses valores caiam nos meses corretos (10, 11, 12), não em jan/fev/mar.
- Decimal brasileiro com sufixo "%": "418,2689 %" → 418.2689.
- Células vazias (meses anteriores ao início ou sem dado) são omitidas.

Depende de `pdfplumber` (mesma dependência usada pelo módulo TJSP).
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
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
class ResultadoJuros:
    """Resultado da importação de uma tabela de juros de mora."""
    tabela_chave: str
    sha256: str
    caminho_arquivo: Path
    taxas: list[tuple[int, int, float]]  # (ano, mes, taxa_percentual)
    mudou: bool                          # False se hash igual ao anterior


# ------------------------------------------------------------------ #
# Hash / arquivamento                                                 #
# ------------------------------------------------------------------ #
def _calcular_sha256(conteudo: bytes) -> str:
    return hashlib.sha256(conteudo).hexdigest()


def _arquivar_pdf(origem: Path, tabela_chave: str, sha256: str) -> Path:
    """
    Copia o PDF de origem para referencias/juros/ com nome:
        {tabela_chave}_AAAA-MM-DD_{sha256[:8]}.pdf
    Preserva o arquivo original (apenas copia).
    """
    config.JUROS_REFERENCIAS_DIR.mkdir(parents=True, exist_ok=True)
    data = datetime.now().strftime("%Y-%m-%d")
    nome = f"{tabela_chave}_{data}_{sha256[:8]}.pdf"
    destino = config.JUROS_REFERENCIAS_DIR / nome
    shutil.copyfile(origem, destino)
    logger.info("JUROS: PDF arquivado em %s", destino)
    return destino


# ------------------------------------------------------------------ #
# Parser                                                              #
# ------------------------------------------------------------------ #
# Linha de dados: a primeira célula é exatamente um ano de 4 dígitos.
_RE_ANO = re.compile(r"^\s*(\d{4})\s*$")


def _br_para_float(s: str) -> Optional[float]:
    """
    '418,2689 %' → 418.2689  |  '1.045,54' → 1045.54

    Retorna None se a célula estiver vazia ou não contiver número.
    """
    if s is None:
        return None
    limpo = s.replace("%", "").strip()
    if not limpo:
        return None
    limpo = limpo.replace(".", "").replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return None


def parsear_pdf(caminho: Path) -> list[tuple[int, int, float]]:
    """
    Lê o PDF da tabela de juros e devolve lista de (ano, mes, taxa),
    com mes em 1..12. Células vazias são omitidas.

    Usa extract_tables() para preservar o alinhamento por coluna:
    coluna 0 = ano, colunas 1..12 = janeiro..dezembro.
    """
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError(
            "pdfplumber não está instalado. Rode: pip install pdfplumber"
        ) from e

    taxas: dict[tuple[int, int], float] = {}

    with pdfplumber.open(str(caminho)) as pdf:
        for pagina in pdf.pages:
            for tabela in pagina.extract_tables():
                for linha in tabela:
                    if not linha or not linha[0]:
                        continue
                    m = _RE_ANO.match(str(linha[0]))
                    if not m:
                        continue
                    ano = int(m.group(1))
                    if not (1900 <= ano <= 2200):
                        continue
                    for mes in range(1, 13):
                        if mes >= len(linha):
                            break
                        valor = _br_para_float(linha[mes])
                        if valor is None:
                            continue
                        # Primeira ocorrência vence (caso a linha apareça
                        # fragmentada entre páginas).
                        taxas.setdefault((ano, mes), valor)

    resultado = [(ano, mes, taxa) for (ano, mes), taxa in taxas.items()]
    resultado.sort(key=lambda x: (x[0], x[1]))
    anos = {ano for ano, _, _ in resultado}
    logger.info(
        "JUROS: %d taxas extraídas (anos %s..%s).",
        len(resultado),
        min(anos) if anos else "-",
        max(anos) if anos else "-",
    )
    return resultado


# ------------------------------------------------------------------ #
# Função principal                                                    #
# ------------------------------------------------------------------ #
def importar_tabela(
    tabela_chave: str,
    arquivo_origem: Path,
    sha256_anterior: Optional[str] = None,
) -> ResultadoJuros:
    """
    Pipeline completo de uma tabela de juros de mora:
    ler PDF local → hash → comparar → (arquivar + parsear se mudou).

    Se o hash for igual ao anterior, retorna ResultadoJuros com
    `mudou=False` e `taxas=[]` (não reprocessa o PDF).
    """
    if not arquivo_origem.is_file():
        raise FileNotFoundError(
            f"PDF de origem não encontrado: {arquivo_origem}. "
            f"Use --origem para apontar a pasta com os PDFs."
        )

    conteudo = arquivo_origem.read_bytes()
    sha256_atual = _calcular_sha256(conteudo)

    if sha256_anterior == sha256_atual:
        logger.info(
            "JUROS %s: hash inalterado (%s). Nada a fazer.",
            tabela_chave, sha256_atual[:8],
        )
        return ResultadoJuros(
            tabela_chave=tabela_chave,
            sha256=sha256_atual,
            caminho_arquivo=Path(""),
            taxas=[],
            mudou=False,
        )

    caminho = _arquivar_pdf(arquivo_origem, tabela_chave, sha256_atual)
    taxas = parsear_pdf(caminho)
    logger.info(
        "JUROS %s: nova versão (%s), %d taxas.",
        tabela_chave, sha256_atual[:8], len(taxas),
    )
    return ResultadoJuros(
        tabela_chave=tabela_chave,
        sha256=sha256_atual,
        caminho_arquivo=caminho,
        taxas=taxas,
        mudou=True,
    )
