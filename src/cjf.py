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
class TabelaCJF:
    """
    Os dois componentes de uma tabela de correção monetária da JF, separados.

    fatores : fator de correção COMPOSTO      -> banco cjf_fatores
    selic   : Selic acumulada LINEAR (em %)   -> banco cjf_selic

    Ambos como (ano, mes, valor). `selic` fica vazio nas tabelas que não têm
    janela de Selic.
    """
    fatores: list[tuple[int, int, float]]
    selic: list[tuple[int, int, float]] = field(default_factory=list)


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
    fatores: list[tuple[int, int, float]]   # correção composta -> cjf_fatores
    selic: list[tuple[int, int, float]]     # Selic linear (%)  -> cjf_selic
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


def _parece_tabela(conteudo: bytes) -> bool:
    """
    O conteúdo é de fato uma tabela do SICOM (e não a home do site, uma
    página de erro ou um arquivo temporário vazio)?

    Checagem deliberadamente barata e explícita: o título da tabela e um
    número mínimo de linhas. Serve para não gravar lixo com hash novo.
    """
    txt = conteudo.decode("latin-1", errors="replace")
    tem_titulo = ("TABELA DE CORRE" in txt.upper()
                  or "VALORES HIST" in txt.upper())
    return tem_titulo and len(re.findall(r"<tr", txt, re.I)) >= 20


def _post_formulario(
    tipo_tabela: str,
    seq_encadeamento: int,
    mes: int,
    ano: int,
) -> bytes:
    """
    Faz POST ao formulário SICOM e devolve o conteúdo do XLS.

    O SICOM serve o arquivo de duas formas, e tentamos nesta ordem:

    1. **Inline na resposta do POST** (comportamento atual, desde ~jul/2026):
       o corpo da resposta JÁ É o arquivo, com `Content-Disposition: inline`.
    2. **Redirect** (comportamento antigo): a resposta é um HTML com
       `window.location.href` apontando para um arquivo em `arquivos/temp/`.

    A tentativa 2 fica como retaguarda caso o SICOM volte atrás. O que NÃO
    fazemos mais é adivinhar a URL do temp por convenção: aquele caminho
    (`arquivos/temp/{ano}_{mes}_{nome}.xls`) hoje responde 200 com a HOME do
    site, o que fazia o parser extrair zero fatores em silêncio.
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
        corpo = resp.read()

    # 1) o arquivo veio inline?
    if _parece_tabela(corpo):
        logger.info(
            "CJF: arquivo recebido inline na resposta do POST (%d bytes).",
            len(corpo),
        )
        return corpo

    # 2) retaguarda: seguir o redirect, se houver
    m = _RE_REDIRECT.search(corpo.decode("latin-1", errors="replace"))
    if not m:
        raise ValueError(
            f"CJF {tipo_tabela}_{seq_encadeamento} ({mes:02d}/{ano}): a resposta "
            f"do POST não é uma tabela ({len(corpo)} bytes) e não traz redirect. "
            f"O formulário do SICOM provavelmente mudou — confira "
            f"{_FORM_URL} antes de confiar na coleta."
        )

    url_arquivo = f"{_BASE_URL}/{m.group(1)}"
    logger.info("CJF: seguindo redirect para %s", url_arquivo)
    req2 = urllib.request.Request(
        url_arquivo, headers={"User-Agent": "calculadora-claims/1.0"}
    )
    with urllib.request.urlopen(req2, timeout=config.TIMEOUT_SEGUNDOS) as resp2:
        conteudo = resp2.read()

    if not _parece_tabela(conteudo):
        raise ValueError(
            f"CJF {tipo_tabela}_{seq_encadeamento} ({mes:02d}/{ano}): o arquivo "
            f"em {url_arquivo} não é uma tabela do SICOM ({len(conteudo)} bytes)."
        )
    return conteudo


# Linha volátil que o SICOM injeta no arquivo com um token novo a cada
# requisição, do tipo:
#     link.download = "2026_08_tabela_de_correcao_monetaria_6a98840f72921";
# Duas chamadas idênticas devolvem 48.436 bytes iguais em tudo, menos nisso.
_RE_TOKEN_VOLATIL = re.compile(
    rb"""link\.download\s*=\s*["'][^"']*["']\s*;""", re.I
)


def _normalizar_para_hash(conteudo: bytes) -> bytes:
    """
    Remove do conteúdo o que muda a cada download sem que a publicação tenha
    mudado. Sem isso o SHA-256 nunca se repete, o hash deixa de detectar
    "sem mudança" e cada execução arquiva outra cópia do mesmo arquivo.

    Normaliza pouco e de forma explícita: só a linha do token. Se o SICOM
    passar a variar outra coisa, o hash volta a mudar sempre — que é o lado
    seguro do erro (rearquiva à toa, mas nunca deixa passar uma mudança real).
    """
    return _RE_TOKEN_VOLATIL.sub(b"", conteudo)


def _calcular_sha256(conteudo: bytes) -> str:
    """SHA-256 do conteúdo normalizado — ver _normalizar_para_hash."""
    return hashlib.sha256(_normalizar_para_hash(conteudo)).hexdigest()


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

# Cabeçalho que abre o componente de Selic: "SELIC de 1/2022 a 9/2025".
_RE_CABECALHO_SELIC = re.compile(r"^\s*SELIC\b", re.I)
# Fim da área de dados.
_RE_FIM_DADOS = re.compile(r"^\s*Observa", re.I)


def _br_para_float(s: str) -> float:
    """'1.045,540000' → 1045.54  |  '0,0075589248' → 0.0075589248"""
    return float(s.replace(".", "").replace(",", "."))


def _extrair_texto_celulas(html: str) -> list[list[str]]:
    """
    Extrai o texto limpo das células <td> de cada linha, PRESERVANDO A POSIÇÃO.

    As células vazias são mantidas na lista (como ""), e isso é essencial: nas
    linhas de dados o mês é a POSIÇÃO da coluna (1 = janeiro ... 12 = dezembro),
    e anos parciais têm colunas vazias. A tabela do CJF começa em out/1964, ou
    seja, a linha de 1964 é ['1964', '', ..., '', v, v, v] — descartar os vazios
    antes de atribuir a posição fazia out/nov/dez de 1964 serem gravados como
    jan/fev/mar. É a mesma armadilha de alinhamento por coluna que já tínhamos
    encontrado nos PDFs de juros de mora.

    Devolve apenas as linhas que tenham ao menos uma célula com conteúdo.
    """
    linhas_texto: list[list[str]] = []
    for row_m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        row_html = row_m.group(1)
        celulas = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL)
        celulas_txt = [re.sub(r"<[^>]+>", "", c).strip() for c in celulas]
        if any(celulas_txt):
            linhas_texto.append(celulas_txt)
    return linhas_texto


def parsear_xls(caminho: Path) -> TabelaCJF:
    """
    Lê o XLS (HTML) do CJF e devolve os DOIS componentes separados.

    As tabelas de correção monetária da JF trazem, no mesmo arquivo e com a
    mesma forma (ano nas linhas, meses nas colunas), dois componentes de
    natureza incompatível:

      `fatores`  fator de correção monetária COMPOSTO   (ex.: 1,0466221456)
      `selic`    percentual de Selic acumulada LINEAR   (ex.: 43,89%)

    O componente de Selic aparece nas categorias com janela de Selic (EC 113
    ou art. 406 do CC) e é aberto por um cabeçalho "SELIC de m/aaaa a m/aaaa".
    Dentro da janela, os fatores do primeiro componente ficam CONGELADOS num
    platô — a correção daquele período está na Selic, não neles.

    Separá-los aqui é o que garante que um percentual nunca chegue ao banco de
    fatores: os dois se sobrepõem no mesmo (ano, mês), e juntos o 43,89 seria
    lido como coeficiente.

    Ignora cabeçalhos, notas de rodapé e células vazias. Dentro de cada
    componente vale a primeira ocorrência de cada ano — é o que trata as
    múltiplas seções do VHI (ORTN, OTN, BTN...), sem cabeçalho identificável.
    """
    conteudo = caminho.read_bytes().decode("latin-1", errors="replace")
    linhas = _extrair_texto_celulas(conteudo)

    fatores: list[tuple[int, int, float]] = []
    selic: list[tuple[int, int, float]] = []
    destino = fatores
    vistos: set[tuple[int, int]] = set()   # (id do destino, ano)
    anos_vistos: set[int] = set()

    for celulas in linhas:
        if not celulas:
            continue

        primeira = celulas[0]

        # Fim da área de dados: começam as observações/rodapé.
        if _RE_FIM_DADOS.match(primeira):
            break

        # Cabeçalho que abre o componente de Selic.
        if _RE_CABECALHO_SELIC.match(primeira):
            destino = selic
            logger.debug("CJF: início do componente de Selic (%s).", primeira[:40])
            continue

        # Linha de dados: primeira célula é exatamente um ano de 4 dígitos
        m = _RE_ANO_CELULA.match(primeira)
        if not m:
            continue
        ano = int(m.group(1))
        if not (1900 <= ano <= 2200):
            continue
        if (id(destino), ano) in vistos:
            continue
        vistos.add((id(destino), ano))
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
                destino.append((ano, mes_idx, valor))

    fatores.sort()
    selic.sort()
    logger.info(
        "CJF: %s — %d fatores de correção e %d valores de Selic (anos %s..%s).",
        caminho.name, len(fatores), len(selic),
        min(anos_vistos) if anos_vistos else "-",
        max(anos_vistos) if anos_vistos else "-",
    )
    if not fatores:
        raise ValueError(
            f"CJF: nenhum fator de correção extraído de {caminho.name}. "
            f"O layout da tabela provavelmente mudou."
        )
    return TabelaCJF(fatores=fatores, selic=selic)


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
            selic=[],
            mudou=False,
        )

    caminho = _salvar_arquivo(conteudo, tabela_chave, sha256_atual)
    tabela = parsear_xls(caminho)
    logger.info(
        "CJF %s: nova versão (%s), %d fatores de correção e %d valores de Selic.",
        tabela_chave, sha256_atual[:8], len(tabela.fatores), len(tabela.selic),
    )
    return ResultadoCJF(
        tabela_chave=tabela_chave,
        tipo_tabela=tipo_tabela,
        seq_encadeamento=seq_encadeamento,
        mes_referencia=mes_referencia,
        ano_referencia=ano_referencia,
        sha256=sha256_atual,
        caminho_arquivo=caminho,
        fatores=tabela.fatores,
        selic=tabela.selic,
        mudou=True,
    )
