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
CJF_REFERENCIAS_DIR = REFERENCIAS_DIR / "cjf"
JUROS_REFERENCIAS_DIR = REFERENCIAS_DIR / "juros"

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

# ------------------------------------------------------------------ #
# Parâmetros do CJF (Tabelas de Correção Monetária - SICOM)          #
# ------------------------------------------------------------------ #
# Referência: https://sicom.cjf.jus.br/tabelaCorMor.php
#
# Campos do formulário:
#   tipoTabela      : "TCM" (Tabela de Correção Monetária)
#                     "VHI" (Valores Históricos dos Indexadores)
#   seqEncadeamento : identificador do tipo de ação judicial
#
# A tabela solicitada reflete sempre a data de referência (mes/ano)
# mais recente informada. Para coleta mensal automatizada, usamos o
# mês corrente.
#
# Chave do dicionário: "{tipoTabela}_{seqEncadeamento}" — usada como
# identificador único nas tabelas do banco e no nome dos arquivos.

CJF_FORM_URL = "https://sicom.cjf.jus.br/tabelaCorMor.php"

# Tipos de ação (seqEncadeamento) disponíveis no SICOM
CJF_SEQ_ENCADEAMENTO = {
    1: "Desapropriações",
    2: "Benefício Previdenciário",
    3: "Ações Condenatórias em Geral (servidores e empregados públicos)",
    4: "Repetição de Indébito Tributário",
    5: "Ações Condenatórias em Geral (Devedor Fazenda Pública)",
    6: "Ações Condenatórias em Geral (devedor não enquadrado como Fazenda Pública)",
}

# Catálogo de combinações (tipoTabela × seqEncadeamento) a coletar.
# Adicione ou remova entradas conforme a necessidade do escritório.
CJF_TABELAS = {
    # TCM — coeficientes de atualização direta (uso mais comum em cálculos)
    "TCM_1": {"tipo_tabela": "TCM", "seq_encadeamento": 1, "nome": "TCM - Desapropriações"},
    "TCM_2": {"tipo_tabela": "TCM", "seq_encadeamento": 2, "nome": "TCM - Benefício Previdenciário"},
    "TCM_3": {"tipo_tabela": "TCM", "seq_encadeamento": 3, "nome": "TCM - Ações Condenatórias (servidores)"},
    "TCM_4": {"tipo_tabela": "TCM", "seq_encadeamento": 4, "nome": "TCM - Repetição de Indébito Tributário"},
    "TCM_5": {"tipo_tabela": "TCM", "seq_encadeamento": 5, "nome": "TCM - Ações Condenatórias (Fazenda Pública)"},
    "TCM_6": {"tipo_tabela": "TCM", "seq_encadeamento": 6, "nome": "TCM - Ações Condenatórias (devedor comum)"},
    # VHI — valores históricos dos indexadores (ORTN, OTN, BTN, etc.)
    "VHI_1": {"tipo_tabela": "VHI", "seq_encadeamento": 1, "nome": "VHI - Desapropriações"},
    "VHI_2": {"tipo_tabela": "VHI", "seq_encadeamento": 2, "nome": "VHI - Benefício Previdenciário"},
    "VHI_3": {"tipo_tabela": "VHI", "seq_encadeamento": 3, "nome": "VHI - Ações Condenatórias (servidores)"},
    "VHI_4": {"tipo_tabela": "VHI", "seq_encadeamento": 4, "nome": "VHI - Repetição de Indébito Tributário"},
    "VHI_5": {"tipo_tabela": "VHI", "seq_encadeamento": 5, "nome": "VHI - Ações Condenatórias (Fazenda Pública)"},
    "VHI_6": {"tipo_tabela": "VHI", "seq_encadeamento": 6, "nome": "VHI - Ações Condenatórias (devedor comum)"},
}

# ------------------------------------------------------------------ #
# Parâmetros das Tabelas de Juros de Mora (importação ESTÁTICA)       #
# ------------------------------------------------------------------ #
# Tabelas de Juros de Mora em Condenações da Fazenda Pública (precatórios).
#
# Diferente do BACEN/TJSP/CJF, estes dados NÃO têm coleta periódica:
# os juros de mora deixaram de ser aplicados a partir de 09/12/2021
# (art. 3º da EC 113/2021). As tabelas são, portanto, HISTÓRICAS e
# fechadas — importadas uma única vez a partir de PDFs locais.
#
# Cada PDF é uma matriz Ano × Mês com a taxa de juros de mora ACUMULADA
# (em %) daquele mês até a data de referência (dez/2021). Os valores
# embutem as mudanças de regime legal ao longo do tempo (0,5% a.m. do
# Código Civil de 1916, 1% a.m. do DL 2.322/1987 e Lei 8.177/1991,
# poupança via Lei 11.960/2009 e EC 62/2009, etc.) — por isso devem ser
# EXTRAÍDOS fielmente, nunca recalculados.
#
# Campos:
#   nome            : descrição curta para relatórios
#   arquivo_origem  : nome do PDF de origem (procurado em --origem)
#   data_referencia : "YYYY-MM" até onde os juros estão acumulados
#   descricao       : descrição longa / categoria
#
# A pasta de ORIGEM dos PDFs é informada via --origem na linha de comando.
# Por padrão, o importador procura na própria pasta de arquivamento
# (referencias/juros/), para que reimportações sejam reprodutíveis a
# partir das cópias já versionadas.
JUROS_FONTE_DIR = JUROS_REFERENCIAS_DIR

# Data de referência comum a todas as tabelas (acumulado até este mês).
JUROS_DATA_REFERENCIA = "2021-12"

JUROS_TABELAS = {
    "JUROS_SERVIDOR": {
        "nome": "Juros de Mora — Servidores e Empregados Públicos",
        "arquivo_origem": "TaxaJuroServidor.pdf",
        "data_referencia": JUROS_DATA_REFERENCIA,
        "descricao": (
            "Taxas de juros de mora acumuladas para precatórios de servidores "
            "e empregados públicos (condenações da Fazenda Pública). Acumulado "
            "até dez/2021; sem juros de mora após 09/12/2021 (art. 3º EC 113/2021)."
        ),
    },
    "JUROS_NAO_TRIBUTARIO": {
        "nome": "Juros de Mora — Precatórios de Natureza Não Tributária",
        "arquivo_origem": "TaxaJuroGeral.pdf",
        "data_referencia": JUROS_DATA_REFERENCIA,
        "descricao": (
            "Taxas de juros de mora acumuladas para precatórios de natureza não "
            "tributária (condenações da Fazenda Pública). Acumulado até dez/2021; "
            "sem juros de mora após 09/12/2021 (art. 3º EC 113/2021)."
        ),
    },
    "JUROS_TRIBUTARIO": {
        "nome": "Juros de Mora — Precatórios Tributários",
        "arquivo_origem": "TaxaJuroTributario.pdf",
        "data_referencia": JUROS_DATA_REFERENCIA,
        "descricao": (
            "Taxas de juros de mora acumuladas para precatórios tributários "
            "(condenações da Fazenda Pública). Acumulado até dez/2021; sem juros "
            "de mora após 09/12/2021 (art. 3º EC 113/2021)."
        ),
    },
}