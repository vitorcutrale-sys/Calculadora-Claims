"""
Gera a calculadora em ARQUIVO ÚNICO: um .html que roda sozinho no Chrome.

Junta num só arquivo:
- a tela (web/calculadora.html);
- o código do motor (src/calculo + src/api.py e o que ele usa);
- o banco de índices (data/claims.db), na versão atual.

Ao abrir o arquivo, o navegador carrega o Pyodide (Python em WebAssembly) e
roda o motor localmente: nenhum dado do cálculo sai da máquina e não há
servidor a manter. Só o Pyodide em si vem da internet (CDN jsDelivr), na
primeira abertura; depois fica no cache do navegador.

Os índices ficam CONGELADOS na data da geração. Para atualizar, rode a coleta
e gere o arquivo de novo.

Uso:
    python gerar_html.py
    python gerar_html.py --saida "C:/caminho/da/pasta/compartilhada/Calculadora.html"
"""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from src import api, config

PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js"
PAGINA = config.BASE_DIR / "web" / "calculadora.html"
SAIDA_PADRAO = config.BASE_DIR / "dist" / "calculadora-precatorios.html"

# Módulos que o motor precisa no navegador (só biblioteca padrão).
MODULOS = [
    "src/__init__.py",
    "src/api.py",
    "src/config.py",
    "src/db.py",
    "src/navegador.py",
    "src/calculo/*.py",
]

# Ponto de inserção: o script principal da tela.
MARCA_SCRIPT = "<script>\n(function () {"

CARREGADOR = """<script src="%(pyodide)s"></script>
<script>
// Motor embutido — gerado por gerar_html.py em %(gerado_em)s.
window.MOTOR_LOCAL = (function () {
  "use strict";
  var RAIZ = "/home/pyodide/app";
  var FONTES = %(fontes)s;
  var BANCO = "%(banco)s";
  // Índices baixados pelo botão "Atualizar índices" ficam guardados neste
  // navegador e são reaplicados a cada abertura, por cima do banco embutido.
  // O caminho normal é o arquivo já nascer atualizado (rotina diária); o
  // botão é a reserva para um dia em que ela não tenha rodado.
  var CHAVE = "calculadora-precatorios:indices";

  function bytes(b64) {
    var bin = atob(b64), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function lerGuardados() {
    try { return JSON.parse(localStorage.getItem(CHAVE)) || {}; } catch (e) { return {}; }
  }

  // Acumula por data: uma nova busca começa só alguns dias antes da última
  // data conhecida, então não pode sobrescrever o que já foi guardado.
  function guardar(codigo, registros) {
    try {
      var g = lerGuardados(), porData = {};
      (g[codigo] || []).concat(registros).forEach(function (r) { porData[r.data] = r; });
      g[codigo] = Object.keys(porData).map(function (k) { return porData[k]; });
      localStorage.setItem(CHAVE, JSON.stringify(g));
    } catch (e) { /* sem armazenamento: vale só até fechar a página */ }
  }

  function py(despachar, rota, corpo) {
    var r = JSON.parse(despachar(rota, corpo === undefined ? "null" : JSON.stringify(corpo)));
    if (!r.ok) throw new Error(r.erro);
    return r.dados;
  }

  var pronto = (typeof loadPyodide === "function"
      ? loadPyodide()
      : Promise.reject(new Error("Pyodide indisponível")))
    .then(function (py) {
      return py.loadPackage("sqlite3").then(function () {
        Object.keys(FONTES).forEach(function (caminho) {
          var completo = RAIZ + "/" + caminho;
          py.FS.mkdirTree(completo.slice(0, completo.lastIndexOf("/")));
          py.FS.writeFile(completo, FONTES[caminho]);
        });
        py.FS.writeFile(RAIZ + "/claims.db", bytes(BANCO));
        BANCO = null;
        return py.runPython(
          "import sys\\n" +
          "sys.path.insert(0, '" + RAIZ + "')\\n" +
          "from src import navegador\\n" +
          "navegador.iniciar('" + RAIZ + "/claims.db')\\n" +
          "navegador.despachar"
        );
      });
    })
    .then(function (despachar) {
      var g = lerGuardados();
      Object.keys(g).forEach(function (codigo) {
        try { py(despachar, "/api/incorporar", { codigo: Number(codigo), registros: g[codigo] }); }
        catch (e) { console.warn("Índices guardados da série " + codigo + " ignorados:", e); }
      });
      return despachar;
    });

  return {
    chamar: function (rota, corpo) {
      return pronto.then(function (despachar) { return py(despachar, rota, corpo); });
    },

    // Busca no BACEN SGS o que falta de cada série e grava no banco. Resolve
    // com { novos, falhas, total }: registros novos ou alterados, os nomes das
    // séries que não vieram e quantas foram consultadas. Uma série que falha
    // não impede as outras.
    atualizarIndices: function () {
      return pronto.then(function (despachar) {
        var novos = 0, falhas = [], pendencias = py(despachar, "/api/pendencias");
        return pendencias.reduce(function (fila, p) {
          // Uma de cada vez, para não somar carga sobre uma API que já oscila.
          return fila.then(function () {
            return buscarSGS(p.url, 3).then(function (registros) {
              novos += py(despachar, "/api/incorporar", { codigo: p.codigo, registros: registros }).novos;
              guardar(p.codigo, registros);
            }).catch(function (e) {
              console.warn("Série " + p.codigo + " não atualizada:", e);
              falhas.push(p.nome);
            });
          });
        }, Promise.resolve()).then(function () {
          return { novos: novos, falhas: falhas, total: pendencias.length };
        });
      });
    }
  };

  // GET no SGS com até `maximo` tentativas e 20 s de limite cada — a API do BACEN
  // oscila (a coleta em Python faz o mesmo, ver bacen._requisitar).
  function buscarSGS(url, maximo, tentativa) {
    tentativa = tentativa || 1;
    var ctl = new AbortController();
    var limite = setTimeout(function () { ctl.abort(); }, 20000);
    return fetch(url, { signal: ctl.signal }).then(function (resp) {
      if (resp.status === 404) return [];   // o SGS responde 404 quando não há dados na janela
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    }).then(function (registros) {
      if (!Array.isArray(registros)) throw new Error("resposta inesperada");
      return registros;
    }).catch(function (e) {
      if (tentativa >= maximo) throw e;
      return new Promise(function (r) { setTimeout(r, 1500 * tentativa); })
        .then(function () { return buscarSGS(url, maximo, tentativa + 1); });
    }).finally(function () { clearTimeout(limite); });
  }
})();
</script>
"""


def _fontes() -> dict[str, str]:
    """Código-fonte dos módulos do motor, por caminho relativo."""
    fontes = {}
    for padrao in MODULOS:
        for arq in sorted(config.BASE_DIR.glob(padrao)):
            rel = arq.relative_to(config.BASE_DIR).as_posix()
            fontes[rel] = arq.read_text(encoding="utf-8")
    return fontes


def _banco() -> bytes:
    """
    Cópia compacta e consistente do banco de índices.

    VACUUM INTO gera um arquivo novo, desfragmentado e sem depender do
    estado do arquivo original (journal, páginas livres).
    """
    with tempfile.TemporaryDirectory() as tmp:
        copia = Path(tmp) / "claims.db"
        conn = sqlite3.connect(config.DB_PATH)
        try:
            conn.execute("VACUUM INTO ?", (str(copia),))
        finally:
            conn.close()
        return copia.read_bytes()


def _status() -> str:
    """Resumo de até quando vão os índices, para o terminal."""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        series = api.status_base(conn)
    finally:
        conn.close()
    return ", ".join(f"{s['nome']} {(s['ultimo_mes'] or '—')[:7]}" for s in series)


def gerar(saida: Path) -> Path:
    if not config.DB_PATH.exists():
        raise SystemExit(
            f"Banco não encontrado em {config.DB_PATH}. Rode antes: python coletar_indices.py"
        )

    pagina = PAGINA.read_text(encoding="utf-8").replace("\r\n", "\n")
    if pagina.count(MARCA_SCRIPT) != 1:
        raise SystemExit("Não encontrei o script principal em web/calculadora.html.")

    # "</" escapado para que nenhum trecho de código feche a tag <script>.
    fontes = json.dumps(_fontes()).replace("</", "<\\/")
    carregador = CARREGADOR % {
        "pyodide": PYODIDE_URL,
        "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "fontes": fontes,
        "banco": base64.b64encode(_banco()).decode("ascii"),
    }

    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(pagina.replace(MARCA_SCRIPT, carregador + MARCA_SCRIPT), encoding="utf-8")
    return saida


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera a calculadora em arquivo único.")
    parser.add_argument("--saida", type=Path, default=SAIDA_PADRAO,
                        help=f"arquivo .html de saída (padrão: {SAIDA_PADRAO})")
    args = parser.parse_args()

    saida = gerar(args.saida)
    print(f"Calculadora gerada em {saida}  ({saida.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"Índices: {_status()}")


if __name__ == "__main__":
    main()
