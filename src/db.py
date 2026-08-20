"""
Camada de persistência em SQLite.

Responsabilidades:
- Criar o schema na primeira execução (idempotente).
- Inserir/atualizar metadados das séries.
- Fazer upsert de observações (codigo_serie, data) → valor.
- Registrar log de cada coleta.
- Expor consultas utilitárias (última data por série, contagens).

Decisões de design:
- Datas armazenadas em ISO 8601 (YYYY-MM-DD) para ordenação correta.
- Valores em REAL (precisão suficiente para índices); cálculos
  financeiros em Decimal devem ser feitos na camada de cálculo,
  re-lendo daqui.
- UNIQUE(codigo_serie, data) garante idempotência: rodar a coleta
  duas vezes não duplica linhas.
"""

from __future__ import annotations
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from . import config

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Schema                                                              #
# ------------------------------------------------------------------ #
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS series (
    codigo            INTEGER PRIMARY KEY,
    nome              TEXT NOT NULL,
    unidade           TEXT,
    periodicidade     TEXT,
    fonte             TEXT,
    descricao         TEXT,
    ativa             INTEGER NOT NULL DEFAULT 1,
    criada_em         TEXT NOT NULL DEFAULT (datetime('now')),
    atualizada_em     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS observacoes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo_serie      INTEGER NOT NULL,
    data              TEXT NOT NULL,          -- YYYY-MM-DD
    valor             REAL NOT NULL,
    coletada_em       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (codigo_serie, data),
    FOREIGN KEY (codigo_serie) REFERENCES series(codigo)
);

CREATE INDEX IF NOT EXISTS idx_obs_serie_data
    ON observacoes(codigo_serie, data);

CREATE TABLE IF NOT EXISTS coletas_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    iniciada_em             TEXT NOT NULL,
    finalizada_em           TEXT,
    status                  TEXT,             -- success | partial | error
    series_processadas      INTEGER DEFAULT 0,
    registros_inseridos     INTEGER DEFAULT 0,
    registros_atualizados   INTEGER DEFAULT 0,
    mensagem                TEXT
);

-- ---------------------------------------------------------------- --
-- Tabelas TJSP                                                      --
-- ---------------------------------------------------------------- --
-- Cada execução baixa o PDF e calcula SHA-256. Se for o mesmo
-- da última versão, não reprocessa (idempotente por hash).
CREATE TABLE IF NOT EXISTS tjsp_publicacoes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tabela_chave    TEXT NOT NULL,          -- chave em config.TJSP_TABELAS
    codigo_arquivo  INTEGER NOT NULL,       -- ex.: 188481
    data_download   TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    caminho_arquivo TEXT NOT NULL,
    UNIQUE (tabela_chave, sha256)
);

-- Fatores extraídos da publicação mais recente, em formato longo.
-- UNIQUE(tabela_chave, ano, mes) garante upsert idempotente quando
-- uma nova versão atualiza os fatores históricos.
CREATE TABLE IF NOT EXISTS tjsp_fatores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tabela_chave    TEXT NOT NULL,
    ano             INTEGER NOT NULL,
    mes             INTEGER NOT NULL,       -- 1..12
    fator           REAL NOT NULL,
    publicacao_id   INTEGER NOT NULL,
    atualizado_em   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tabela_chave, ano, mes),
    FOREIGN KEY (publicacao_id) REFERENCES tjsp_publicacoes(id)
);

CREATE INDEX IF NOT EXISTS idx_tjsp_fatores_anomes
    ON tjsp_fatores(tabela_chave, ano, mes);

-- ---------------------------------------------------------------- --
-- Tabelas CJF (SICOM - Correção Monetária)                         --
-- ---------------------------------------------------------------- --
-- Registra cada download do SICOM. Hash SHA-256 evita reprocessamento.
CREATE TABLE IF NOT EXISTS cjf_publicacoes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tabela_chave        TEXT NOT NULL,   -- ex.: "TCM_3", "VHI_2"
    tipo_tabela         TEXT NOT NULL,   -- "TCM" ou "VHI"
    seq_encadeamento    INTEGER NOT NULL,-- 1..6
    mes_referencia      INTEGER NOT NULL,-- mês da tabela (1..12)
    ano_referencia      INTEGER NOT NULL,-- ano da tabela
    data_download       TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    caminho_arquivo     TEXT NOT NULL,
    UNIQUE (tabela_chave, sha256)
);

-- Coeficientes/valores extraídos da publicação mais recente.
-- UNIQUE(tabela_chave, ano, mes) garante upsert idempotente.
CREATE TABLE IF NOT EXISTS cjf_fatores (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tabela_chave        TEXT NOT NULL,
    ano                 INTEGER NOT NULL,
    mes                 INTEGER NOT NULL,   -- 1..12
    valor               REAL NOT NULL,
    publicacao_id       INTEGER NOT NULL,
    atualizado_em       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tabela_chave, ano, mes),
    FOREIGN KEY (publicacao_id) REFERENCES cjf_publicacoes(id)
);

CREATE INDEX IF NOT EXISTS idx_cjf_fatores_anomes
    ON cjf_fatores(tabela_chave, ano, mes);

-- ---------------------------------------------------------------- --
-- Tabelas de Juros de Mora (importação ESTÁTICA / histórica)        --
-- ---------------------------------------------------------------- --
-- Juros de mora em condenações da Fazenda Pública (precatórios).
-- Dados fechados: deixaram de ser aplicados em 09/12/2021 (EC 113/2021).
-- Importados uma única vez a partir de PDFs locais; SHA-256 garante
-- idempotência caso a importação seja repetida.
CREATE TABLE IF NOT EXISTS juros_publicacoes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tabela_chave    TEXT NOT NULL,       -- chave em config.JUROS_TABELAS
    descricao       TEXT,
    data_referencia TEXT,                -- "YYYY-MM": acumulado até este mês
    data_importacao TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    caminho_arquivo TEXT NOT NULL,
    UNIQUE (tabela_chave, sha256)
);

-- Taxas de juros de mora ACUMULADAS (em %), em formato longo.
-- UNIQUE(tabela_chave, ano, mes) garante upsert idempotente.
CREATE TABLE IF NOT EXISTS juros_fatores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tabela_chave    TEXT NOT NULL,
    ano             INTEGER NOT NULL,
    mes             INTEGER NOT NULL,    -- 1..12
    taxa            REAL NOT NULL,       -- % de juros de mora acumulado
    publicacao_id   INTEGER NOT NULL,
    atualizado_em   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tabela_chave, ano, mes),
    FOREIGN KEY (publicacao_id) REFERENCES juros_publicacoes(id)
);

CREATE INDEX IF NOT EXISTS idx_juros_fatores_anomes
    ON juros_fatores(tabela_chave, ano, mes);

-- ---------------------------------------------------------------- --
-- Tabela Progressiva Mensal do IRPF (Receita Federal)              --
-- ---------------------------------------------------------------- --
-- Coleta ANUAL da página do ano-calendário. Cada página pode conter
-- mais de uma vigência (a tabela muda no meio do ano em alguns anos).
CREATE TABLE IF NOT EXISTS irpf_publicacoes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ano_pagina      INTEGER NOT NULL,   -- ano-calendário da página de origem
    fonte_url       TEXT NOT NULL,
    data_coleta     TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    caminho_arquivo TEXT NOT NULL,      -- HTML arquivado em referencias/irpf/
    UNIQUE (ano_pagina, sha256)
);

-- Faixas da tabela mensal, por vigência.
-- `aliquota` é gravada COMO PUBLICADA, em pontos percentuais (7.5 = 7,5%),
-- seguindo o mesmo padrão das séries BACEN em `observacoes`; a conversão
-- para fração é feita na camada de cálculo.
-- `base_ate` NULL = última faixa (sem teto).
-- `vigencia_fim` NULL = vigente por prazo indeterminado.
CREATE TABLE IF NOT EXISTS irpf_faixas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vigencia_inicio TEXT NOT NULL,      -- YYYY-MM-DD
    vigencia_fim    TEXT,               -- YYYY-MM-DD ou NULL
    ordem           INTEGER NOT NULL,   -- 1..5
    base_de         REAL NOT NULL,
    base_ate        REAL,
    aliquota        REAL NOT NULL,      -- pontos percentuais
    deducao         REAL NOT NULL,      -- parcela a deduzir (R$)
    publicacao_id   INTEGER NOT NULL,
    atualizado_em   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (vigencia_inicio, ordem),
    FOREIGN KEY (publicacao_id) REFERENCES irpf_publicacoes(id)
);

CREATE INDEX IF NOT EXISTS idx_irpf_faixas_vigencia
    ON irpf_faixas(vigencia_inicio, ordem);
"""


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #
@contextmanager
def conectar(db_path: Optional[Path] = None):
    """Context manager que abre conexão com SQLite e garante commit/rollback."""
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Foreign keys precisam ser ativadas por conexão no SQLite.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def inicializar_schema(db_path: Optional[Path] = None) -> None:
    """Cria tabelas e índices se ainda não existem. Idempotente."""
    with conectar(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
    logger.info("Schema inicializado em %s", db_path or config.DB_PATH)


# ------------------------------------------------------------------ #
# Séries (metadados)                                                  #
# ------------------------------------------------------------------ #
def sincronizar_serie(codigo: int, meta: dict, conn: sqlite3.Connection) -> None:
    """Insere ou atualiza os metadados de uma série."""
    conn.execute(
        """
        INSERT INTO series (codigo, nome, unidade, periodicidade, fonte, descricao)
        VALUES (:codigo, :nome, :unidade, :periodicidade, :fonte, :descricao)
        ON CONFLICT(codigo) DO UPDATE SET
            nome          = excluded.nome,
            unidade       = excluded.unidade,
            periodicidade = excluded.periodicidade,
            fonte         = excluded.fonte,
            descricao     = excluded.descricao,
            atualizada_em = datetime('now')
        """,
        {"codigo": codigo, **meta},
    )


def sincronizar_todas_series(conn: sqlite3.Connection) -> None:
    for codigo, meta in config.SERIES.items():
        sincronizar_serie(codigo, meta, conn)


def listar_series_ativas(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT codigo, nome FROM series WHERE ativa = 1 ORDER BY codigo"
    )
    return cur.fetchall()


# ------------------------------------------------------------------ #
# Observações                                                         #
# ------------------------------------------------------------------ #
def ultima_data(conn: sqlite3.Connection, codigo: int) -> Optional[str]:
    """Retorna a data ISO mais recente já gravada para a série, ou None."""
    cur = conn.execute(
        "SELECT MAX(data) AS m FROM observacoes WHERE codigo_serie = ?",
        (codigo,),
    )
    row = cur.fetchone()
    return row["m"] if row and row["m"] else None


def upsert_observacoes(
    conn: sqlite3.Connection,
    codigo: int,
    observacoes: Iterable[tuple[str, float]],
) -> tuple[int, int]:
    """
    Insere ou atualiza observações para uma série.

    `observacoes` é um iterável de tuplas (data_iso, valor).
    Retorna (inseridas, atualizadas).
    """
    inseridas = 0
    atualizadas = 0
    for data_iso, valor in observacoes:
        # Tenta INSERT; em caso de conflito, faz UPDATE só se mudou o valor.
        cur = conn.execute(
            """
            INSERT INTO observacoes (codigo_serie, data, valor)
            VALUES (?, ?, ?)
            ON CONFLICT(codigo_serie, data) DO UPDATE SET
                valor = excluded.valor,
                coletada_em = datetime('now')
            WHERE observacoes.valor != excluded.valor
            """,
            (codigo, data_iso, valor),
        )
        # rowcount == 1 quando INSERT ou UPDATE acontece; 0 quando ON CONFLICT
        # com condição WHERE falsa (valor inalterado).
        if cur.rowcount == 1:
            # Distingue insert de update consultando se já existia antes seria
            # caro; usamos a heurística: se a linha existia, era UPDATE.
            # Simplificação: tratamos rowcount==1 como "afetada".
            inseridas += 1
    return inseridas, atualizadas


# ------------------------------------------------------------------ #
# Log de coletas                                                      #
# ------------------------------------------------------------------ #
def iniciar_log_coleta(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO coletas_log (iniciada_em, status) VALUES (?, 'running')",
        (datetime.now().isoformat(timespec="seconds"),),
    )
    return cur.lastrowid


def finalizar_log_coleta(
    conn: sqlite3.Connection,
    log_id: int,
    status: str,
    series_processadas: int,
    registros_inseridos: int,
    mensagem: str = "",
) -> None:
    conn.execute(
        """
        UPDATE coletas_log
           SET finalizada_em       = ?,
               status              = ?,
               series_processadas  = ?,
               registros_inseridos = ?,
               mensagem            = ?
         WHERE id = ?
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            status,
            series_processadas,
            registros_inseridos,
            mensagem,
            log_id,
        ),
    )


# ------------------------------------------------------------------ #
# TJSP                                                                #
# ------------------------------------------------------------------ #
def ultimo_sha256_tjsp(
    conn: sqlite3.Connection, tabela_chave: str
) -> Optional[str]:
    """Hash SHA-256 da publicação TJSP mais recente para essa tabela."""
    cur = conn.execute(
        """
        SELECT sha256 FROM tjsp_publicacoes
         WHERE tabela_chave = ?
         ORDER BY id DESC LIMIT 1
        """,
        (tabela_chave,),
    )
    row = cur.fetchone()
    return row["sha256"] if row else None


def registrar_publicacao_tjsp(
    conn: sqlite3.Connection,
    tabela_chave: str,
    codigo_arquivo: int,
    sha256: str,
    caminho_arquivo: str,
) -> int:
    """Insere registro de publicação e retorna seu id."""
    cur = conn.execute(
        """
        INSERT INTO tjsp_publicacoes
            (tabela_chave, codigo_arquivo, data_download, sha256, caminho_arquivo)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            tabela_chave,
            codigo_arquivo,
            datetime.now().isoformat(timespec="seconds"),
            sha256,
            caminho_arquivo,
        ),
    )
    return cur.lastrowid


def upsert_fatores_tjsp(
    conn: sqlite3.Connection,
    tabela_chave: str,
    publicacao_id: int,
    fatores: Iterable[tuple[int, int, float]],
) -> int:
    """
    Insere ou atualiza fatores (ano, mes, fator).

    Retorna a quantidade de linhas afetadas.
    """
    afetadas = 0
    for ano, mes, fator in fatores:
        cur = conn.execute(
            """
            INSERT INTO tjsp_fatores
                (tabela_chave, ano, mes, fator, publicacao_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tabela_chave, ano, mes) DO UPDATE SET
                fator         = excluded.fator,
                publicacao_id = excluded.publicacao_id,
                atualizado_em = datetime('now')
            WHERE tjsp_fatores.fator != excluded.fator
            """,
            (tabela_chave, ano, mes, fator, publicacao_id),
        )
        if cur.rowcount == 1:
            afetadas += 1
    return afetadas


def resumo_tjsp(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Resumo por tabela TJSP: contagem, ano mínimo e máximo de fatores."""
    cur = conn.execute(
        """
        SELECT
            tabela_chave,
            COUNT(*)        AS n_fatores,
            MIN(ano)        AS primeiro_ano,
            MAX(ano)        AS ultimo_ano
        FROM tjsp_fatores
        GROUP BY tabela_chave
        ORDER BY tabela_chave
        """
    )
    return cur.fetchall()


# ------------------------------------------------------------------ #
# CJF                                                                 #
# ------------------------------------------------------------------ #
def ultimo_sha256_cjf(
    conn: sqlite3.Connection, tabela_chave: str
) -> Optional[str]:
    """SHA-256 da publicação CJF mais recente para essa chave."""
    cur = conn.execute(
        """
        SELECT sha256 FROM cjf_publicacoes
         WHERE tabela_chave = ?
         ORDER BY id DESC LIMIT 1
        """,
        (tabela_chave,),
    )
    row = cur.fetchone()
    return row["sha256"] if row else None


def registrar_publicacao_cjf(
    conn: sqlite3.Connection,
    tabela_chave: str,
    tipo_tabela: str,
    seq_encadeamento: int,
    mes_referencia: int,
    ano_referencia: int,
    sha256: str,
    caminho_arquivo: str,
) -> int:
    """Insere registro de publicação CJF e retorna seu id."""
    cur = conn.execute(
        """
        INSERT INTO cjf_publicacoes
            (tabela_chave, tipo_tabela, seq_encadeamento,
             mes_referencia, ano_referencia, data_download, sha256, caminho_arquivo)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tabela_chave,
            tipo_tabela,
            seq_encadeamento,
            mes_referencia,
            ano_referencia,
            datetime.now().isoformat(timespec="seconds"),
            sha256,
            caminho_arquivo,
        ),
    )
    return cur.lastrowid


def upsert_fatores_cjf(
    conn: sqlite3.Connection,
    tabela_chave: str,
    publicacao_id: int,
    fatores: Iterable[tuple[int, int, float]],
) -> int:
    """Insere ou atualiza fatores CJF (ano, mes, valor). Retorna linhas afetadas."""
    afetadas = 0
    for ano, mes, valor in fatores:
        cur = conn.execute(
            """
            INSERT INTO cjf_fatores
                (tabela_chave, ano, mes, valor, publicacao_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tabela_chave, ano, mes) DO UPDATE SET
                valor         = excluded.valor,
                publicacao_id = excluded.publicacao_id,
                atualizado_em = datetime('now')
            WHERE cjf_fatores.valor != excluded.valor
            """,
            (tabela_chave, ano, mes, valor, publicacao_id),
        )
        if cur.rowcount == 1:
            afetadas += 1
    return afetadas


def resumo_cjf(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Resumo por tabela CJF: contagem, ano mínimo e máximo de fatores."""
    cur = conn.execute(
        """
        SELECT
            tabela_chave,
            COUNT(*)        AS n_fatores,
            MIN(ano)        AS primeiro_ano,
            MAX(ano)        AS ultimo_ano
        FROM cjf_fatores
        GROUP BY tabela_chave
        ORDER BY tabela_chave
        """
    )
    return cur.fetchall()


# ------------------------------------------------------------------ #
# Juros de Mora (importação estática)                                 #
# ------------------------------------------------------------------ #
def ultimo_sha256_juros(
    conn: sqlite3.Connection, tabela_chave: str
) -> Optional[str]:
    """SHA-256 da publicação de juros mais recente para essa chave."""
    cur = conn.execute(
        """
        SELECT sha256 FROM juros_publicacoes
         WHERE tabela_chave = ?
         ORDER BY id DESC LIMIT 1
        """,
        (tabela_chave,),
    )
    row = cur.fetchone()
    return row["sha256"] if row else None


def registrar_publicacao_juros(
    conn: sqlite3.Connection,
    tabela_chave: str,
    descricao: str,
    data_referencia: str,
    sha256: str,
    caminho_arquivo: str,
) -> int:
    """Insere registro de publicação de juros e retorna seu id."""
    cur = conn.execute(
        """
        INSERT INTO juros_publicacoes
            (tabela_chave, descricao, data_referencia,
             data_importacao, sha256, caminho_arquivo)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            tabela_chave,
            descricao,
            data_referencia,
            datetime.now().isoformat(timespec="seconds"),
            sha256,
            caminho_arquivo,
        ),
    )
    return cur.lastrowid


def upsert_fatores_juros(
    conn: sqlite3.Connection,
    tabela_chave: str,
    publicacao_id: int,
    taxas: Iterable[tuple[int, int, float]],
) -> int:
    """Insere ou atualiza taxas de juros (ano, mes, taxa). Retorna linhas afetadas."""
    afetadas = 0
    for ano, mes, taxa in taxas:
        cur = conn.execute(
            """
            INSERT INTO juros_fatores
                (tabela_chave, ano, mes, taxa, publicacao_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tabela_chave, ano, mes) DO UPDATE SET
                taxa          = excluded.taxa,
                publicacao_id = excluded.publicacao_id,
                atualizado_em = datetime('now')
            WHERE juros_fatores.taxa != excluded.taxa
            """,
            (tabela_chave, ano, mes, taxa, publicacao_id),
        )
        if cur.rowcount == 1:
            afetadas += 1
    return afetadas


def resumo_juros(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Resumo por tabela de juros: contagem, ano mínimo e máximo."""
    cur = conn.execute(
        """
        SELECT
            tabela_chave,
            COUNT(*)        AS n_fatores,
            MIN(ano)        AS primeiro_ano,
            MAX(ano)        AS ultimo_ano
        FROM juros_fatores
        GROUP BY tabela_chave
        ORDER BY tabela_chave
        """
    )
    return cur.fetchall()


# ------------------------------------------------------------------ #
# IRPF — Tabela Progressiva Mensal                                    #
# ------------------------------------------------------------------ #
def ultimo_sha256_irpf(conn: sqlite3.Connection, ano_pagina: int) -> Optional[str]:
    """SHA-256 da última coleta da página daquele ano."""
    cur = conn.execute(
        """
        SELECT sha256 FROM irpf_publicacoes
         WHERE ano_pagina = ?
         ORDER BY id DESC LIMIT 1
        """,
        (ano_pagina,),
    )
    row = cur.fetchone()
    return row["sha256"] if row else None


def registrar_publicacao_irpf(
    conn: sqlite3.Connection,
    ano_pagina: int,
    fonte_url: str,
    sha256: str,
    caminho_arquivo: str,
) -> int:
    """Insere registro de publicação do IRPF e retorna seu id."""
    cur = conn.execute(
        """
        INSERT INTO irpf_publicacoes
            (ano_pagina, fonte_url, data_coleta, sha256, caminho_arquivo)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            ano_pagina,
            fonte_url,
            datetime.now().isoformat(timespec="seconds"),
            sha256,
            caminho_arquivo,
        ),
    )
    return cur.lastrowid


def upsert_faixas_irpf(
    conn: sqlite3.Connection,
    publicacao_id: int,
    tabelas: Iterable,
) -> int:
    """
    Insere ou atualiza as faixas de cada tabela (objetos irpf.TabelaIRPF).

    Retorna a quantidade de linhas afetadas. UNIQUE(vigencia_inicio, ordem)
    garante idempotência: recoletar a mesma página não duplica.
    """
    afetadas = 0
    for tabela in tabelas:
        vig_ini = tabela.vigencia_inicio.isoformat()
        vig_fim = tabela.vigencia_fim.isoformat() if tabela.vigencia_fim else None
        for f in tabela.faixas:
            cur = conn.execute(
                """
                INSERT INTO irpf_faixas
                    (vigencia_inicio, vigencia_fim, ordem, base_de, base_ate,
                     aliquota, deducao, publicacao_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vigencia_inicio, ordem) DO UPDATE SET
                    vigencia_fim  = excluded.vigencia_fim,
                    base_de       = excluded.base_de,
                    base_ate      = excluded.base_ate,
                    aliquota      = excluded.aliquota,
                    deducao       = excluded.deducao,
                    publicacao_id = excluded.publicacao_id,
                    atualizado_em = datetime('now')
                WHERE irpf_faixas.base_de   != excluded.base_de
                   OR irpf_faixas.aliquota  != excluded.aliquota
                   OR irpf_faixas.deducao   != excluded.deducao
                   OR IFNULL(irpf_faixas.base_ate, -1) != IFNULL(excluded.base_ate, -1)
                """,
                (
                    vig_ini, vig_fim, f.ordem, f.base_de, f.base_ate,
                    f.aliquota, f.deducao, publicacao_id,
                ),
            )
            if cur.rowcount == 1:
                afetadas += 1
    return afetadas


def tabela_irpf_vigente(
    conn: sqlite3.Connection, quando: str
) -> list[sqlite3.Row]:
    """
    Faixas da tabela mensal vigente na data `quando` (ISO YYYY-MM-DD).

    Escolhe a vigência mais recente que já começou até essa data.
    """
    cur = conn.execute(
        """
        SELECT * FROM irpf_faixas
         WHERE vigencia_inicio = (
                 SELECT MAX(vigencia_inicio) FROM irpf_faixas
                  WHERE vigencia_inicio <= ?
               )
         ORDER BY ordem
        """,
        (quando,),
    )
    return cur.fetchall()


def resumo_irpf(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Resumo por vigência: nº de faixas e limite de isenção."""
    cur = conn.execute(
        """
        SELECT
            vigencia_inicio,
            vigencia_fim,
            COUNT(*)                                  AS n_faixas,
            MAX(CASE WHEN ordem = 1 THEN base_ate END) AS isencao_ate
        FROM irpf_faixas
        GROUP BY vigencia_inicio, vigencia_fim
        ORDER BY vigencia_inicio
        """
    )
    return cur.fetchall()


# ------------------------------------------------------------------ #
# Diagnóstico                                                         #
# ------------------------------------------------------------------ #
def resumo(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Tabela resumo: por série, contagem e min/max de datas."""
    cur = conn.execute(
        """
        SELECT
            s.codigo,
            s.nome,
            COUNT(o.id)    AS n_obs,
            MIN(o.data)    AS primeira,
            MAX(o.data)    AS ultima
        FROM series s
        LEFT JOIN observacoes o ON o.codigo_serie = s.codigo
        WHERE s.ativa = 1
        GROUP BY s.codigo, s.nome
        ORDER BY s.codigo
        """
    )
    return cur.fetchall()
