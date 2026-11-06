"""
Ponte entre a tela e o motor quando o Python roda no próprio navegador.

No arquivo único gerado por `gerar_html.py`, o motor roda no Pyodide (Python
compilado para WebAssembly) e não há servidor HTTP. A tela chama
`despachar(rota, corpo_json)` com as mesmas rotas do `servidor.py` e recebe
de volta um JSON no formato:

    {"ok": true,  "dados": ...}      sucesso
    {"ok": false, "erro": "..."}     erro, com mensagem para o usuário

Atualização de índices: o motor só lê dados "vivos" de séries do BACEN SGS
(as tabelas do CJF e de juros param em dez/2021). A API do SGS aceita
chamadas de qualquer origem (CORS), então a própria página busca o que falta:

    /api/pendencias  -> URLs a buscar, a partir do que o banco já tem
    /api/incorporar  -> grava no banco (em memória) a resposta de uma URL

A busca em si fica no JavaScript (fetch do navegador); aqui só se decide o
que buscar e se grava, com as mesmas funções de upsert da coleta.

Só biblioteca padrão: é o que garante que o mesmo código rode no Pyodide.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
from datetime import date, datetime, timedelta

from . import api, config, db

_conn: sqlite3.Connection | None = None

# Séries BACEN que o motor lê (correção, Selic e juros). A poupança (195) é
# tratada à parte porque cada registro é um período, com `dataFim`.
SERIES_ATUALIZAVEIS = (433, 7478, 4390, 29543)

# Retrocesso na data inicial, para reapanhar revisões — o mesmo da coleta.
RETROCESSO_SERIES = timedelta(days=7)
RETROCESSO_POUPANCA = timedelta(days=40)


def _br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _iso(data_br: str) -> str:
    return datetime.strptime(data_br, "%d/%m/%Y").date().isoformat()


def _url(codigo: int, inicio: date) -> str:
    return (f"{config.BACEN_BASE_URL}/bcdata.sgs.{codigo}/dados"
            f"?formato=json&dataInicial={_br(inicio)}&dataFinal={_br(date.today())}")


def pendencias() -> list[dict]:
    """URLs do SGS a buscar para trazer cada série até hoje."""
    saida = []
    for codigo in SERIES_ATUALIZAVEIS:
        ultima = db.ultima_data(_conn, codigo)
        inicio = date.fromisoformat(ultima) - RETROCESSO_SERIES
        saida.append({"codigo": codigo, "nome": api.SERIES_STATUS.get(codigo, str(codigo)),
                      "url": _url(codigo, inicio)})
    ultima = db.ultima_data_poupanca(_conn)
    inicio = date.fromisoformat(ultima) - RETROCESSO_POUPANCA
    saida.append({"codigo": config.POUPANCA_SERIE, "nome": "Poupança",
                  "url": _url(config.POUPANCA_SERIE, inicio)})
    return saida


def incorporar(codigo: int, registros: list) -> int:
    """
    Grava no banco os registros de uma resposta do SGS. Idempotente: pode
    receber de novo o que já está gravado. Retorna as linhas novas ou alteradas.
    """
    if codigo == config.POUPANCA_SERIE:
        periodos = [
            (_iso(r["data"]), _iso(r["dataFim"]), float(r["valor"]))
            for r in registros if r.get("dataFim")
        ]
        afetadas = db.upsert_poupanca_periodos(_conn, periodos)
    elif codigo in SERIES_ATUALIZAVEIS:
        obs = [(_iso(r["data"]), float(r["valor"])) for r in registros]
        afetadas = sum(db.upsert_observacoes(_conn, codigo, obs))
    else:
        raise api.ErroEntrada(f"Série {codigo} não é atualizável pela calculadora.")
    _conn.commit()
    return afetadas


def iniciar(caminho_db: str) -> None:
    """Abre (somente para leitura) o banco de índices embutido no arquivo."""
    global _conn
    _conn = sqlite3.connect(caminho_db)
    _conn.row_factory = sqlite3.Row


def _resposta(**campos) -> str:
    return json.dumps(campos, ensure_ascii=False)


def despachar(rota: str, corpo_json: str = "null") -> str:
    """Atende uma chamada da tela. Nunca levanta: erros viram `ok: false`."""
    try:
        if rota == "/api/perfis":
            dados = api.listar_perfis()
        elif rota == "/api/status":
            dados = api.status_base(_conn)
        elif rota == "/api/calcular":
            corpo = json.loads(corpo_json)
            if not isinstance(corpo, dict):
                raise api.ErroEntrada("Corpo da requisição não é um JSON válido.")
            dados = api.calcular(_conn, corpo)
        elif rota == "/api/pendencias":
            dados = pendencias()
        elif rota == "/api/incorporar":
            corpo = json.loads(corpo_json)
            dados = {"novos": incorporar(int(corpo["codigo"]), corpo["registros"])}
        else:
            return _resposta(ok=False, erro="Rota não encontrada.")
    except api.ErroEntrada as e:
        return _resposta(ok=False, erro=str(e))
    except Exception:
        # Vai para o console do navegador (F12), para a equipe diagnosticar.
        traceback.print_exc()
        return _resposta(ok=False, erro="Erro inesperado no cálculo.")
    return _resposta(ok=True, dados=dados)
