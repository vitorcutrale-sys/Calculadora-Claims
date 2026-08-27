"""
Cliente HTTP para a API SGS do BACEN.

A API expõe cada série temporal em:
    https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados
        ?formato=json
        [&dataInicial=DD/MM/AAAA]
        [&dataFinal=DD/MM/AAAA]

Resposta: JSON array de objetos `{"data": "DD/MM/AAAA", "valor": "0.42"}`.

Para séries diárias o BACEN limita ~10 anos por requisição. Quebramos
o intervalo em janelas de CHUNK_ANOS anos para sermos compatíveis com
qualquer periodicidade.

Usamos apenas a stdlib (urllib + json) — zero dependências externas.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

from . import config

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Conversões de data                                                  #
# ------------------------------------------------------------------ #
def br_para_iso(data_br: str) -> str:
    """'31/12/2024' → '2024-12-31'."""
    return datetime.strptime(data_br, "%d/%m/%Y").date().isoformat()


def iso_para_br(data_iso: str) -> str:
    """'2024-12-31' → '31/12/2024'."""
    return datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%Y")


def date_para_br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


# ------------------------------------------------------------------ #
# Chunking de intervalos                                              #
# ------------------------------------------------------------------ #
def gerar_janelas(
    data_inicio_br: str,
    data_fim_br: Optional[str] = None,
    anos_por_janela: int = config.CHUNK_ANOS,
) -> Iterator[tuple[str, str]]:
    """
    Gera tuplas (inicio_br, fim_br) em janelas de até `anos_por_janela`.

    Se data_fim_br for None, usa hoje.
    """
    inicio = datetime.strptime(data_inicio_br, "%d/%m/%Y").date()
    fim = (
        datetime.strptime(data_fim_br, "%d/%m/%Y").date()
        if data_fim_br
        else date.today()
    )
    if inicio > fim:
        return

    janela_inicio = inicio
    while janela_inicio <= fim:
        # Aproxima janela_inicio + anos_por_janela anos. Usar timedelta
        # com 365*anos é seguro para chunking (não precisa ser exato).
        janela_fim = min(
            janela_inicio + timedelta(days=365 * anos_por_janela),
            fim,
        )
        yield date_para_br(janela_inicio), date_para_br(janela_fim)
        janela_inicio = janela_fim + timedelta(days=1)


# ------------------------------------------------------------------ #
# Chamada HTTP                                                        #
# ------------------------------------------------------------------ #
def _montar_url(
    codigo: int,
    data_inicio_br: Optional[str] = None,
    data_fim_br: Optional[str] = None,
) -> str:
    params: dict[str, str] = {"formato": "json"}
    if data_inicio_br:
        params["dataInicial"] = data_inicio_br
    if data_fim_br:
        params["dataFinal"] = data_fim_br
    query = urllib.parse.urlencode(params)
    return f"{config.BACEN_BASE_URL}/bcdata.sgs.{codigo}/dados?{query}"


def _requisitar(url: str) -> list[dict]:
    """
    GET + parse JSON, com retries e backoff exponencial.

    A API retorna:
      - 200 + JSON array com dados (pode ser vazio se sem dados no período)
      - 404 ou similar se a série não existe
      - 503/timeout em caso de instabilidade
    """
    ultima_excecao: Optional[Exception] = None
    for tentativa in range(1, config.MAX_TENTATIVAS + 1):
        try:
            logger.debug("GET %s (tentativa %d)", url, tentativa)
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "calculadora-claims/1.0"},
            )
            with urllib.request.urlopen(
                req, timeout=config.TIMEOUT_SEGUNDOS
            ) as resp:
                payload = resp.read().decode("utf-8")
                # Resposta vazia ocorre se não há dados no intervalo.
                if not payload.strip():
                    return []
                return json.loads(payload)
        except urllib.error.HTTPError as e:
            # 404 = série inexistente: não adianta tentar de novo.
            if e.code in (400, 404):
                raise
            ultima_excecao = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            ultima_excecao = e

        if tentativa < config.MAX_TENTATIVAS:
            espera = config.BACKOFF_BASE ** tentativa
            logger.warning(
                "Falha na tentativa %d (%s). Aguardando %.1fs.",
                tentativa,
                ultima_excecao,
                espera,
            )
            time.sleep(espera)

    raise RuntimeError(
        f"Falha ao baixar {url} após {config.MAX_TENTATIVAS} tentativas: "
        f"{ultima_excecao}"
    )


# ------------------------------------------------------------------ #
# Função principal                                                    #
# ------------------------------------------------------------------ #
def baixar_serie(
    codigo: int,
    data_inicio_br: Optional[str] = None,
    data_fim_br: Optional[str] = None,
) -> list[tuple[str, float]]:
    """
    Baixa observações de uma série em janelas.

    Retorna lista de tuplas (data_iso, valor) ordenadas por data.

    Se `data_inicio_br` for None, usa config.DATA_HISTORICO_INICIAL.

    Tratamento de 404 por janela:
    o BACEN responde 404 quando a série existe mas não tem dados na
    janela pedida (típico de janelas anteriores ao início da série,
    ex.: TR antes de 1991). Nesse caso pulamos a janela e seguimos
    com as próximas — só consideramos a série inválida se TODAS as
    janelas retornarem 404.
    """
    inicio = data_inicio_br or config.DATA_HISTORICO_INICIAL
    observacoes: list[tuple[str, float]] = []
    janelas_ok = 0
    janelas_404 = 0

    for ini, fim in gerar_janelas(inicio, data_fim_br):
        url = _montar_url(codigo, ini, fim)
        logger.info("Série %d: baixando %s → %s", codigo, ini, fim)
        try:
            dados = _requisitar(url)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                # Janela anterior ao início da série ou sem dados.
                # Não derruba a série inteira — só registra e segue.
                logger.warning(
                    "Série %d: janela %s → %s sem dados (HTTP %d). "
                    "Continuando para a próxima janela.",
                    codigo, ini, fim, e.code,
                )
                janelas_404 += 1
                continue
            raise

        janelas_ok += 1
        for item in dados:
            try:
                data_iso = br_para_iso(item["data"])
                valor = float(item["valor"])
                observacoes.append((data_iso, valor))
            except (KeyError, ValueError) as e:
                logger.warning(
                    "Série %d: ignorando registro inválido %r (%s)",
                    codigo,
                    item,
                    e,
                )

    # Se nenhuma janela retornou OK E todas falharam com 404,
    # a série provavelmente não existe no SGS.
    if janelas_ok == 0 and janelas_404 > 0:
        raise RuntimeError(
            f"Série {codigo}: todas as {janelas_404} janelas retornaram 404. "
            f"O código provavelmente não existe no SGS — confirme em "
            f"https://www3.bcb.gov.br/sgspub."
        )

    # A API geralmente retorna ordenado, mas garantimos.
    observacoes.sort(key=lambda x: x[0])

    # Deduplica caso janelas sobreponham (no fim/início de janela).
    vistos: set[str] = set()
    deduplicadas: list[tuple[str, float]] = []
    for data_iso, valor in observacoes:
        if data_iso in vistos:
            continue
        vistos.add(data_iso)
        deduplicadas.append((data_iso, valor))

    logger.info(
        "Série %d: %d observações baixadas (período %s → %s). "
        "Janelas OK: %d, sem dados (404): %d.",
        codigo,
        len(deduplicadas),
        deduplicadas[0][0] if deduplicadas else "-",
        deduplicadas[-1][0] if deduplicadas else "-",
        janelas_ok,
        janelas_404,
    )
    return deduplicadas


def baixar_serie_com_data_fim(
    codigo: int,
    data_inicio_br: Optional[str] = None,
    data_fim_br: Optional[str] = None,
) -> list[tuple[str, str, float]]:
    """
    Variante de `baixar_serie` para séries em que cada registro é um
    PERÍODO — o SGS devolve, além de `data` e `valor`, um `dataFim`.

    É o caso da poupança por data de aniversário (série 195):

        {"data": "26/08/2026", "dataFim": "26/09/2026", "valor": "0.6718"}

    Retorna lista de tuplas (data_inicio_iso, data_fim_iso, valor),
    ordenada por data de início e sem duplicidades. Registros sem
    `dataFim` são ignorados com aviso — sinal de que a série não tem essa
    natureza e deveria usar `baixar_serie`.
    """
    inicio = data_inicio_br or config.DATA_HISTORICO_INICIAL
    periodos: list[tuple[str, str, float]] = []
    janelas_ok = 0
    janelas_404 = 0

    for ini, fim in gerar_janelas(inicio, data_fim_br):
        url = _montar_url(codigo, ini, fim)
        logger.info("Série %d: baixando %s → %s", codigo, ini, fim)
        try:
            dados = _requisitar(url)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                logger.warning(
                    "Série %d: janela %s → %s sem dados (HTTP %d). "
                    "Continuando para a próxima janela.",
                    codigo, ini, fim, e.code,
                )
                janelas_404 += 1
                continue
            raise

        janelas_ok += 1
        for item in dados:
            try:
                if not item.get("dataFim"):
                    logger.warning(
                        "Série %d: registro sem dataFim %r — ignorado.", codigo, item
                    )
                    continue
                periodos.append(
                    (
                        br_para_iso(item["data"]),
                        br_para_iso(item["dataFim"]),
                        float(item["valor"]),
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning(
                    "Série %d: ignorando registro inválido %r (%s)", codigo, item, e
                )

    if janelas_ok == 0 and janelas_404 > 0:
        raise RuntimeError(
            f"Série {codigo}: todas as {janelas_404} janelas retornaram 404. "
            f"Confirme o código em https://www3.bcb.gov.br/sgspub."
        )

    periodos.sort(key=lambda x: x[0])
    vistos: set[str] = set()
    deduplicados: list[tuple[str, str, float]] = []
    for data_ini, data_fim, valor in periodos:
        if data_ini in vistos:
            continue
        vistos.add(data_ini)
        deduplicados.append((data_ini, data_fim, valor))

    logger.info(
        "Série %d: %d períodos baixados (%s → %s).",
        codigo,
        len(deduplicados),
        deduplicados[0][0] if deduplicados else "-",
        deduplicados[-1][0] if deduplicados else "-",
    )
    return deduplicados
