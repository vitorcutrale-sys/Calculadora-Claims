"""
Configuração central do projeto.

Define caminhos do banco de dados, dos logs e o catálogo de séries
econômicas do BACEN SGS que serão coletadas.

Para acrescentar ou remover séries, edite o dicionário SERIES abaixo.
Os metadados (nome, unidade, fonte) são gravados na tabela `series`
do banco no primeiro carregamento; alterações posteriores podem ser
sincronizadas executando `coletar_indices.py --sync-metadados`.
"""

from pathlib import Path

# ------------------------------------------------------------------ #
# Caminhos                                                            #
# ------------------------------------------------------------------ #
# Diretório raiz do projeto (pasta que contém /src)
BASE_DIR = Path(__file__).resolve().parent.parent

# Pasta para o banco SQLite. Criada automaticamente se não existir.
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "claims.db"

# Pasta para logs. Criada automaticamente.
LOG_DIR = BASE_DIR / "logs"

# Pasta para arquivos de referência (PDFs de tabelas oficiais etc.)
REFERENCIAS_DIR = BASE_DIR / "referencias"
TJSP_REFERENCIAS_DIR = REFERENCIAS_DIR / "tjsp"

# ------------------------------------------------------------------ #
# Catálogo de séries do BACEN SGS                                     #
# ------------------------------------------------------------------ #
# Referência oficial: https://www3.bcb.gov.br/sgspub
#
# Campos:
#   nome           : descrição curta usada nos relatórios
#   unidade        : "% a.m.", "% a.a.", "índice", etc.
#   periodicidade  : "diaria", "mensal", "anual"
#   fonte          : entidade que apura (IBGE, FGV, BCB)
#   descricao      : descrição longa
#
# IMPORTANTE: a descrição da série 7811 está marcada como "A confirmar".
# Recomendamos abrir o portal do SGS (link acima) e validar antes de
# usar em cálculos oficiais — a IDs próximas no SGS podem mudar de
# significado conforme o BACEN reorganiza séries.

SERIES = {
    433: {
        "nome": "IPCA",
        "unidade": "% a.m.",
        "periodicidade": "mensal",
        "fonte": "IBGE",
        "descricao": "Índice Nacional de Preços ao Consumidor Amplo - variação mensal",
    },
    188: {
        "nome": "IGP-DI",
        "unidade": "% a.m.",
        "periodicidade": "mensal",
        "fonte": "FGV",
        "descricao": "Índice Geral de Preços - Disponibilidade Interna - variação mensal",
    },
    189: {
        "nome": "IGP-M",
        "unidade": "% a.m.",
        "periodicidade": "mensal",
        "fonte": "FGV",
        "descricao": "Índice Geral de Preços - Mercado - variação mensal",
    },
    196: {
        "nome": "Poupança",
        "unidade": "% a.m.",
        "periodicidade": "mensal",
        "fonte": "BCB",
        "descricao": "Poupança com TR - variação mensal",
    },
    4390: {
        "nome": "Selic mensal",
        "unidade": "% a.m.",
        "periodicidade": "mensal",
        "fonte": "BCB",
        "descricao": "Taxa Selic acumulada no mês",
    },
    4391: {
        "nome": "CDI mensal",
        "unidade": "% a.m.",
        "periodicidade": "mensal",
        "fonte": "BCB",
        "descricao": "CDI acumulado no mês",
    },
    7811: {
        "nome": "TR - Taxa Referencial",
        "unidade": "%a.m.",
        "periodicidade": "mensal",
        "fonte": "BCB",
        "descricao": "TR - Taxa Referencial acumulada no mês",
    },
    7478:{
        "nome":"IPCA-E",
        "unidade": "%a.m.",
        "periodicidade":"mensal",
        "fonte":"BCB",
        "descricao":"Indice Nacional de Preços ao Consumidor Amplo"
    }
}

# ------------------------------------------------------------------ #
# Parâmetros da API BACEN                                             #
# ------------------------------------------------------------------ #
BACEN_BASE_URL = "https://api.bcb.gov.br/dados/serie"

# Data mínima para carga histórica completa. O BACEN ignora datas
# anteriores ao início de cada série, então usar uma data muito antiga
# é seguro.
DATA_HISTORICO_INICIAL = "01/01/1980"

# A API do BACEN limita séries diárias a ~10 anos por chamada.
# Quebramos a requisição em janelas de até CHUNK_ANOS anos para
# garantir compatibilidade com todas as periodicidades.
CHUNK_ANOS = 10

# Timeout em segundos para cada chamada HTTP.
TIMEOUT_SEGUNDOS = 60

# Número de tentativas em caso de falha transitória.
MAX_TENTATIVAS = 3

# Tempo base (segundos) para backoff exponencial entre tentativas.
BACKOFF_BASE = 2.0

# ------------------------------------------------------------------ #
# Parâmetros do TJSP (Tabelas Práticas)                               #
# ------------------------------------------------------------------ #
# As Tabelas Práticas do TJSP são publicadas como PDFs servidos por
# um handler ASP.NET. Cada tabela é identificada por um "código" no
# CMS do tribunal.
#
# Endpoint:
#   https://api.tjsp.jus.br/Handlers/Handler/FileFetch.ashx?codigo={cod}
#
# URL alternativo direto (mesmo arquivo, nome do arquivo no path):
#   https://www.tjsp.jus.br/Download/Tabelas/TabelaEmendaConstitucional113-2021.pdf

TJSP_BASE_URL = "https://api.tjsp.jus.br/Handlers/Handler/FileFetch.ashx"

# Catálogo de tabelas TJSP a baixar.
# Para acrescentar uma tabela nova, basta adicionar uma entrada.
TJSP_TABELAS = {
    "EC113_2021": {
        "codigo": 188481,
        "nome": "Tabela Prática EC 113/2021",
        "descricao": (
            "Fatores acumulados de atualização monetária para débitos "
            "da Fazenda Pública conforme EC 113/2021 (Selic isolada "
            "desde 12/2021)."
        ),
    },
}