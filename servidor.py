"""
Servidor local da interface da calculadora.

Serve a tela (web/calculadora.html) e expõe o motor de cálculo por HTTP,
usando só a biblioteca padrão — sem framework.

Uso:
    python servidor.py              # http://127.0.0.1:8000
    python servidor.py --porta 8080

Rotas:
    GET  /                 a tela da calculadora
    GET  /api/perfis       tipos de ativo disponíveis
    GET  /api/status       até que mês a base de índices está atualizada
    POST /api/calcular     roda o cálculo (corpo e resposta em JSON)

Escuta apenas em 127.0.0.1: é para uso na própria máquina. Em produção, o
mesmo contrato de src/api.py vai atrás do API Gateway + Lambda.
"""

from __future__ import annotations

import argparse
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src import api, config, db

PAGINA = config.BASE_DIR / "web" / "calculadora.html"
LIMITE_CORPO = 64 * 1024   # o input de um cálculo cabe com folga em 64 KB

logger = logging.getLogger("servidor")


class Handler(BaseHTTPRequestHandler):
    server_version = "CalculadoraClaims/1.0"

    # ---- respostas ---- #
    def _enviar(self, status: HTTPStatus, corpo: bytes, tipo: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, status: HTTPStatus, dados) -> None:
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self._enviar(status, corpo, "application/json; charset=utf-8")

    def _erro(self, status: HTTPStatus, mensagem: str) -> None:
        self._json(status, {"erro": mensagem})

    # ---- rotas ---- #
    def do_GET(self) -> None:
        if self.path in ("/", "/index.html", "/calculadora.html"):
            self._enviar(HTTPStatus.OK, PAGINA.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/perfis":
            self._json(HTTPStatus.OK, api.listar_perfis())
        elif self.path == "/api/status":
            with db.conectar() as conn:
                self._json(HTTPStatus.OK, api.status_base(conn))
        else:
            self._erro(HTTPStatus.NOT_FOUND, "Rota não encontrada.")

    def do_POST(self) -> None:
        if self.path != "/api/calcular":
            self._erro(HTTPStatus.NOT_FOUND, "Rota não encontrada.")
            return

        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho <= 0 or tamanho > LIMITE_CORPO:
            self._erro(HTTPStatus.BAD_REQUEST, "Corpo da requisição ausente ou grande demais.")
            return
        try:
            payload = json.loads(self.rfile.read(tamanho))
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            self._erro(HTTPStatus.BAD_REQUEST, "Corpo da requisição não é um JSON válido.")
            return

        try:
            with db.conectar() as conn:
                resultado = api.calcular(conn, payload)
        except api.ErroEntrada as e:
            self._erro(HTTPStatus.BAD_REQUEST, str(e))
            return
        except Exception:
            logger.exception("Falha inesperada no cálculo")
            self._erro(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Erro inesperado no cálculo. Consulte o log do servidor.",
            )
            return
        self._json(HTTPStatus.OK, resultado)

    def log_message(self, formato: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), formato % args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Servidor local da calculadora.")
    parser.add_argument("--porta", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not config.DB_PATH.exists():
        raise SystemExit(
            f"Banco não encontrado em {config.DB_PATH}. Rode antes: python coletar_indices.py"
        )

    servidor = ThreadingHTTPServer(("127.0.0.1", args.porta), Handler)
    print(f"Calculadora disponível em http://127.0.0.1:{args.porta}  (Ctrl+C para encerrar)")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
