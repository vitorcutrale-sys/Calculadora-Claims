# Calculadora de Claims — Coleta de Índices

Projeto para construir a base de dados de índices econômicos aplicáveis a precatórios, alimentada por **três fontes oficiais**:

- **BACEN SGS** — séries diárias/mensais de IPCA, IGP-DI, IGP-M, Selic, CDI, TR, IPCA-E e Poupança, via API JSON.
- **TJSP — Tabelas Práticas** — PDFs oficiais com fatores acumulados de atualização monetária (atualmente, a Tabela da EC 113/2021).
- **CJF — SICOM** — Tabelas de Correção Monetária e Valores Históricos dos Indexadores do Conselho da Justiça Federal, para as 6 categorias de ação judicial, via formulário web mensal.

O script Python baixa, persiste e mantém atualizado tudo em SQLite local, com auditoria por hash de cada versão dos arquivos baixados.

---

## Estrutura do projeto

```
Calculadora-Claims/
├── coletar_indices.py           Entry point unificado (BACEN + TJSP + CJF)
├── executar_coleta.bat          Wrapper diário invocado pelo Task Scheduler
├── executar_coleta_cjf.bat      Wrapper mensal CJF invocado pelo Task Scheduler
├── agendar_windows.bat          Registra a rotina diária no Task Scheduler
├── agendar_cjf_mensal.bat       Registra a rotina mensal CJF no Task Scheduler
├── README.md                    Este arquivo
├── src/
│   ├── config.py                Caminhos, séries BACEN, tabelas TJSP e CJF
│   ├── bacen.py                 Cliente HTTP da API SGS (BACEN)
│   ├── tjsp.py                  Download + parser das tabelas TJSP (PDF)
│   ├── cjf.py                   Download + parser das tabelas CJF (XLS/HTML)
│   ├── db.py                    Camada SQLite (BACEN + TJSP + CJF)
│   └── coletor.py               Orquestrador (BACEN + TJSP + CJF)
├── data/
│   └── claims.db                Banco SQLite (criado na 1ª execução)
├── referencias/
│   ├── tjsp/
│   │   └── EC113_2021_AAAA-MM-DD_<hash>.pdf   Versões arquivadas dos PDFs TJSP
│   └── cjf/
│       └── TCM_3_AAAA-MM-DD_<hash>.xls        Versões arquivadas dos XLS CJF
└── logs/
    ├── coleta_AAAA-MM-DD.log    Logs diários do Python
    ├── agendador.log            Saída do wrapper diário
    └── agendador_cjf.log        Saída do wrapper mensal CJF
```

## Séries coletadas

| Código | Nome                     | Unidade   | Fonte | Periodicidade |
|-------:|--------------------------|-----------|-------|---------------|
| 433    | IPCA                     | % a.m.    | IBGE  | mensal        |
| 188    | IGP-DI                   | % a.m.    | FGV   | mensal        |
| 189    | IGP-M                    | % a.m.    | FGV   | mensal        |
| 196    | Poupança                 | % a.m.    | BCB   | mensal        |
| 4390   | Selic mensal             | % a.m.    | BCB   | mensal        |
| 4391   | CDI mensal               | % a.m.    | BCB   | mensal        |
| 7478   | IPCA-E                   | % a.m.    | BCB   | mensal        |
| 7811   | TR - Taxa Referencial    | % a.m.    | BCB   | mensal        |

Para acrescentar, remover ou alterar séries, edite o dicionário `SERIES` em `src/config.py` e rode `python coletar_indices.py` novamente — os metadados serão sincronizados automaticamente na tabela `series` do banco.

## Tabelas TJSP coletadas

| Chave         | Código no CMS | Descrição                                       |
|---------------|--------------:|-------------------------------------------------|
| EC113_2021    | 188481        | Tabela Prática EC 113/2021 (Selic isolada)      |

Endpoint: `https://api.tjsp.jus.br/Handlers/Handler/FileFetch.ashx?codigo={código}`.

Cada execução baixa o PDF, calcula o SHA-256 e só reprocessa quando há mudança. Os PDFs ficam preservados em `referencias/tjsp/` com o hash no nome.

Para acrescentar uma nova tabela TJSP, edite o dicionário `TJSP_TABELAS` em `src/config.py`.

## Tabelas CJF coletadas

O CJF (Conselho da Justiça Federal) publica, via [SICOM](https://sicom.cjf.jus.br/tabelaCorMor.php), dois tipos de tabela para **6 categorias de ação judicial**:

| Tipo | Descrição |
|------|-----------|
| **TCM** | Tabela de Correção Monetária — coeficientes de atualização direta |
| **VHI** | Valores Históricos dos Indexadores — valores nominais de ORTN, OTN, BTN, UFIR, IPCA-E, Selic etc. |

| `seqEncadeamento` | Tipo de ação |
|:-----------------:|-------------|
| 1 | Desapropriações |
| 2 | Benefício Previdenciário |
| 3 | Ações Condenatórias em Geral (servidores e empregados públicos) |
| 4 | Repetição de Indébito Tributário |
| 5 | Ações Condenatórias em Geral (Devedor Fazenda Pública) |
| 6 | Ações Condenatórias em Geral (devedor não enquadrado como Fazenda Pública) |

São coletadas **12 combinações** (TCM × 6 + VHI × 6), totalizando ~8.400 fatores históricos de 1964 até o mês corrente.

A coleta é **mensal** (as tabelas são publicadas uma vez por mês). O arquivo XLS é um HTML gerado dinamicamente pelo formulário SICOM — o script faz POST ao formulário, segue o redirect e baixa o arquivo gerado. SHA-256 garante que só há reprocessamento quando o CJF publica uma nova versão.

Para adicionar ou remover combinações, edite o dicionário `CJF_TABELAS` em `src/config.py`.

## Pré-requisitos

- Python 3.10 ou superior (verifique com `python --version`)
- Conexão de internet (APIs BACEN e TJSP são públicas, sem autenticação)
- Bibliotecas externas:
  - `pdfplumber` — para extrair os fatores das tabelas em PDF do TJSP

```cmd
pip install pdfplumber
```

(O módulo BACEN continua usando apenas a stdlib; `pdfplumber` é necessário apenas para o módulo TJSP.)

## Primeira execução (carga histórica completa)

Abra o PowerShell ou Prompt de Comando na pasta do projeto e rode:

```cmd
python coletar_indices.py
```

Na primeira vez ele detecta que o banco está vazio e baixa o histórico completo de todas as séries desde 01/01/1980 (datas anteriores ao início real de cada série são ignoradas pelo BACEN). Espere alguns minutos.

Ao final, será impresso um resumo:

```
=== Resumo do banco de índices ===
   Cód | Nome                         |     Obs. | 1ª data    | Última
----------------------------------------------------------------------------
   188 | IGP-DI                       |      500 | 1980-01-01 | 2026-04-01
   189 | IGP-M                        |      500 | 1989-06-01 | 2026-04-01
   ...
```

O arquivo de banco fica em `data/claims.db`.

## Execuções subsequentes (incremental)

A partir da segunda execução, o script detecta automaticamente que já existe histórico e faz apenas a carga incremental — retrocedendo 7 dias da última data conhecida para reapanhar eventuais revisões retroativas do BACEN.

```cmd
python coletar_indices.py                      # padrão: BACEN auto + TJSP + CJF
python coletar_indices.py --resumo             # apenas mostra o resumo do banco
python coletar_indices.py --completo           # força recarga histórica BACEN
python coletar_indices.py --so-bacen           # roda apenas a coleta BACEN
python coletar_indices.py --so-tjsp            # roda apenas a coleta TJSP
python coletar_indices.py --so-cjf             # roda apenas a coleta CJF (mês corrente)
python coletar_indices.py --so-cjf --mes 5 --ano 2025  # CJF para mês/ano específico
```

## Agendar rotinas automáticas (Windows Task Scheduler)

São duas rotinas independentes:

### Rotina diária — BACEN + TJSP

1. Clique com o botão direito em `agendar_windows.bat`.
2. Escolha **Executar como administrador**.
3. Registra a tarefa `CalculadoraClaims_ColetaIndicesBACEN` para rodar todo dia às **13:00**.

```cmd
schtasks /Query  /TN "CalculadoraClaims_ColetaIndicesBACEN" /V /FO LIST
schtasks /Run    /TN "CalculadoraClaims_ColetaIndicesBACEN"
schtasks /Delete /TN "CalculadoraClaims_ColetaIndicesBACEN" /F
```

### Rotina mensal — CJF

1. Clique com o botão direito em `agendar_cjf_mensal.bat`.
2. Escolha **Executar como administrador**.
3. Registra a tarefa `CalculadoraClaims_ColetaCJF_Mensal` para rodar no **dia 5 de cada mês às 08:00** (o CJF costuma publicar as tabelas do mês corrente nos primeiros dias do mês).

```cmd
schtasks /Query  /TN "CalculadoraClaims_ColetaCJF_Mensal" /V /FO LIST
schtasks /Run    /TN "CalculadoraClaims_ColetaCJF_Mensal"
schtasks /Delete /TN "CalculadoraClaims_ColetaCJF_Mensal" /F
```

Os logs ficam em `logs/agendador.log` (wrapper diário), `logs/agendador_cjf.log` (wrapper mensal) e `logs/coleta_AAAA-MM-DD.log` (logs estruturados do Python).

## Estrutura do banco

**Tabela `series`** — metadados de cada série SGS

| coluna         | tipo    |
|----------------|---------|
| codigo (PK)    | INTEGER |
| nome           | TEXT    |
| unidade        | TEXT    |
| periodicidade  | TEXT    |
| fonte          | TEXT    |
| descricao      | TEXT    |
| ativa          | INTEGER |
| criada_em      | TEXT    |
| atualizada_em  | TEXT    |

**Tabela `observacoes`** — valores por data

| coluna         | tipo    |
|----------------|---------|
| id (PK)        | INTEGER |
| codigo_serie   | INTEGER → series.codigo |
| data           | TEXT (YYYY-MM-DD) |
| valor          | REAL    |
| coletada_em    | TEXT    |

`UNIQUE (codigo_serie, data)` — rodar a coleta duas vezes nunca duplica.

**Tabela `coletas_log`** — auditoria de cada execução

Registra início, fim, status (`success` / `partial` / `error`), contagens e mensagem.

**Tabela `tjsp_publicacoes`** — versões dos PDFs baixados do TJSP

| coluna         | tipo    |
|----------------|---------|
| id (PK)        | INTEGER |
| tabela_chave   | TEXT (ex.: "EC113_2021") |
| codigo_arquivo | INTEGER (ex.: 188481)   |
| data_download  | TEXT    |
| sha256         | TEXT (hash do PDF)      |
| caminho_arquivo| TEXT (path em referencias/tjsp/) |

`UNIQUE (tabela_chave, sha256)` — mesma versão do PDF não duplica.

**Tabela `tjsp_fatores`** — fatores acumulados extraídos do TJSP

| coluna         | tipo    |
|----------------|---------|
| id (PK)        | INTEGER |
| tabela_chave   | TEXT    |
| ano            | INTEGER |
| mes            | INTEGER (1..12) |
| fator          | REAL    |
| publicacao_id  | INTEGER → tjsp_publicacoes.id |
| atualizado_em  | TEXT    |

`UNIQUE (tabela_chave, ano, mes)` — uma linha por mês/ano em cada tabela.

**Tabela `cjf_publicacoes`** — versões dos XLS baixados do CJF

| coluna            | tipo    |
|-------------------|---------|
| id (PK)           | INTEGER |
| tabela_chave      | TEXT (ex.: "TCM_3", "VHI_2") |
| tipo_tabela       | TEXT ("TCM" ou "VHI") |
| seq_encadeamento  | INTEGER (1..6) |
| mes_referencia    | INTEGER |
| ano_referencia    | INTEGER |
| data_download     | TEXT    |
| sha256            | TEXT    |
| caminho_arquivo   | TEXT (path em referencias/cjf/) |

`UNIQUE (tabela_chave, sha256)` — mesma versão não duplica.

**Tabela `cjf_fatores`** — coeficientes/valores extraídos do CJF

| coluna         | tipo    |
|----------------|---------|
| id (PK)        | INTEGER |
| tabela_chave   | TEXT    |
| ano            | INTEGER |
| mes            | INTEGER (1..12) |
| valor          | REAL    |
| publicacao_id  | INTEGER → cjf_publicacoes.id |
| atualizado_em  | TEXT    |

`UNIQUE (tabela_chave, ano, mes)` — uma linha por mês/ano em cada tabela.

## Consultar o banco

Você pode abrir `data/claims.db` em qualquer ferramenta SQLite. Recomendo:

- [DB Browser for SQLite](https://sqlitebrowser.org/) — visual, gratuito
- Via Python:
  ```python
  import sqlite3
  conn = sqlite3.connect("data/claims.db")
  for row in conn.execute(
      "SELECT data, valor FROM observacoes "
      "WHERE codigo_serie = 433 ORDER BY data DESC LIMIT 5"
  ):
      print(row)
  ```

## Importante para uso em cálculos de precatórios

Os valores no banco são armazenados como `REAL` (double-precision float, ~15 dígitos significativos). Isso é suficiente para guardar os índices fielmente, mas para **cálculos financeiros** (multiplicações encadeadas de fatores de correção) os valores devem ser carregados em `Decimal` no Python:

```python
from decimal import Decimal, getcontext
getcontext().prec = 28
fator = Decimal(str(valor_lido_do_banco))
```

Isso evita o acúmulo de erros de ponto flutuante em séries longas — fundamental quando o claim final pode ser de milhões.

## Próximos passos do projeto

1. ✅ Ingestão diária de índices BACEN (8 séries)
2. ✅ Ingestão de Tabelas Práticas do TJSP (EC 113/2021)
3. ✅ Ingestão mensal das Tabelas CJF — SICOM (TCM + VHI × 6 categorias)
4. ⏭️ Motor de cálculo de fatores acumulados entre datas (BACEN, TJSP e CJF)
5. ⏭️ Importação da planilha-protótipo de cálculos
6. ⏭️ Interface (CLI ou Streamlit) para rodar cálculos
7. ⏭️ Definir usuários finais e segurança/auditoria