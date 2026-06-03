# Jornada de Desenvolvimento — Calculadora de Claims

Este documento é o registro narrativo do que foi construído, por que foi construído daquela forma, e o que aprendemos ao longo do caminho. Diferente do `README.md` (que documenta o **estado atual** do projeto para quem chega novo), este arquivo conta a **história** do desenvolvimento.

Última atualização: **2026-06-03**.

---

## Sumário

1. [Contexto e objetivo](#1-contexto-e-objetivo)
2. [Decisões fundacionais](#2-decisões-fundacionais)
3. [Fase 1 — Ingestão de índices do BACEN SGS](#3-fase-1--ingestão-de-índices-do-bacen-sgs)
4. [Lições aprendidas na Fase 1](#4-lições-aprendidas-na-fase-1)
5. [Agendamento via Windows Task Scheduler](#5-agendamento-via-windows-task-scheduler)
6. [Fase 2 — Ingestão das Tabelas Práticas do TJSP](#6-fase-2--ingestão-das-tabelas-práticas-do-tjsp)
7. [Fase 3 — Ingestão das Tabelas CJF (Justiça Federal)](#7-fase-3--ingestão-das-tabelas-cjf-justiça-federal)
8. [Estado atual do projeto](#8-estado-atual-do-projeto)
9. [Próximas fases](#9-próximas-fases)

---

## 1. Contexto e objetivo

A JIVE Investments atua na aquisição e recuperação de créditos judiciais — principalmente precatórios e ativos estressados. Cálculos de atualização monetária e juros, nessa atividade, são o coração da avaliação dos ativos: um erro de centésimos pode representar diferenças de milhões num portfólio.

O ponto de partida deste projeto foi um **protótipo em planilha Excel** já em uso, que faz os cálculos manualmente. O objetivo é transformá-lo em **software** — começando pela base mais crítica e que é fonte recorrente de retrabalho: uma base de dados local, atualizada automaticamente, dos índices oficiais aplicáveis.

A entrega final deste projeto, no horizonte, é uma **calculadora completa de claims tributários e precatórios** com motor próprio de cálculo, interface de uso e auditoria.

## 2. Decisões fundacionais

Antes de escrever uma linha de código, três decisões guiaram tudo o que veio depois:

**Banco de dados: SQLite.** Não é "banco de pobre" — é literalmente o mais usado do mundo. Para o nosso caso (uso interno, equipe pequena, sem concorrência alta), elimina semanas de configuração de servidor e ainda permite migrar para PostgreSQL no futuro sem dor (basta usar SQLAlchemy quando precisar). O arquivo `data/claims.db` é portátil, versionável e cabe num backup do OneDrive.

**Linguagem: Python.** Stdlib é poderosa o suficiente para HTTP + JSON + SQL, e é a linguagem onde o Vitor consegue ler e modificar código sem precisar virar engenheiro. Dependências externas só quando estritamente necessário — o que valeu por toda a Fase 1.

**Arquitetura: separação por responsabilidade.** Em vez de um único script de mil linhas, dividi em módulos pequenos:
- `config.py` — só configuração (nomes, códigos, paths)
- `bacen.py` / `tjsp.py` — só clientes de cada fonte externa
- `db.py` — só persistência
- `coletor.py` — só orquestração
- `coletar_indices.py` — só CLI (entry point)

Essa separação não é arbitrária: ela permite testar cada peça isoladamente e trocar uma fonte sem mexer nas outras. Quando expandirmos para a fase de cálculo, essa estrutura já está pronta para receber `calculadora.py`, `relatorios.py` etc.

## 3. Fase 1 — Ingestão de índices do BACEN SGS

### Objetivo

Construir um banco SQLite com o histórico completo de **8 séries econômicas** do BACEN (Sistema Gerenciador de Séries Temporais — SGS), atualizado automaticamente todo dia.

### Séries coletadas

| Código | Nome | Fonte |
|-------:|------|-------|
| 433 | IPCA | IBGE |
| 188 | IGP-DI | FGV |
| 189 | IGP-M | FGV |
| 196 | Poupança | BCB |
| 4390 | Selic mensal | BCB |
| 4391 | CDI mensal | BCB |
| 7478 | IPCA-E | BCB |
| 7811 | TR - Taxa Referencial | BCB |

A escolha dessas séries específicas reflete o universo de cálculos de precatórios e claims tributários: IPCA e IPCA-E para atualização monetária, IGP-DI/IGP-M para contratos antigos, Selic e CDI para juros, TR para certos regimes, Poupança para outros.

### A API SGS

Endpoint público, sem autenticação:

```
https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados?formato=json
    [&dataInicial=DD/MM/AAAA&dataFinal=DD/MM/AAAA]
```

Resposta JSON simples: `[{"data": "01/01/2024", "valor": "0.42"}, ...]`. Datas em formato BR (DD/MM/AAAA), valores como string com ponto decimal.

### Schema SQLite

Três tabelas formam a espinha dorsal da ingestão BACEN:

- **`series`** — metadados de cada série (nome, unidade, fonte, descrição)
- **`observacoes`** — pares (data, valor) por série, com `UNIQUE(codigo_serie, data)` garantindo idempotência
- **`coletas_log`** — auditoria de cada execução (início, fim, status, contagens)

### Estratégia de coleta

**Carga inicial completa:** quando o banco está vazio, baixa todo o histórico desde 01/01/1980 (datas anteriores ao início real de cada série são ignoradas pelo BACEN).

**Carga incremental:** nas execuções seguintes, detecta a última data por série e baixa apenas a partir dali — retrocedendo 7 dias para reapanhar eventuais **revisões retroativas** que o BACEN aplica em alguns índices.

**Chunking:** o BACEN limita séries diárias a ~10 anos por requisição. Quebrei em janelas de 10 anos para sermos compatíveis com qualquer periodicidade — funciona tanto para mensais (rápido) quanto para diárias (lento mas confiável).

**Idempotência por upsert:** usei `INSERT ... ON CONFLICT(codigo_serie, data) DO UPDATE SET valor = excluded.valor WHERE valor != excluded.valor` — significa que rodar a coleta 10 vezes no mesmo dia produz o mesmo resultado, sem duplicar nem perder revisões.

## 4. Lições aprendidas na Fase 1

Durante a primeira carga real (rodada no computador do Vitor), descobrimos que **as séries 196 e 7811 não retornavam valores**, embora as outras cinco estivessem perfeitas.

### Como diagnosticamos

Em vez de eu mexer no código direto, o Vitor pediu para eu apontar **onde** poderia estar o problema, para aprender. Listei três hipóteses, em ordem de probabilidade:

1. Os códigos não eram o que o Vitor pensava (rótulos no `config.py` são decorativos — não mudam o que a API retorna)
2. O intervalo de data começava antes da série existir
3. Codificação da URL com `%2F`

A confirmação veio quando o Vitor testou a URL direta no navegador: **funcionava**. Isso eliminou as hipóteses 1 e 3 e isolou a causa na hipótese 2.

### A causa real

Meu tratamento original de erro no `bacen.py` era **binário**: qualquer `HTTPError` com código 400/404 abortava a série inteira:

```python
except urllib.error.HTTPError as e:
    if e.code in (400, 404):
        raise   # ← derruba a série inteira
```

O BACEN, na prática, devolve **404 para "série existe mas não tem dados nesta janela"** — não só para "série inexistente". Como a TR foi criada em 1991 e a Poupança em 1996, a primeira janela (1980-1990) levantava 404 antes de chegar nas janelas com dados.

### A correção (e a lição)

Mudei o tratamento de "binário por série" para "granular por janela":

```python
except urllib.error.HTTPError as e:
    if e.code in (400, 404):
        logger.warning("Janela sem dados, continuando.")
        janelas_404 += 1
        continue   # ← pula só a janela, segue para a próxima
    raise
```

E adicionei uma checagem final: se TODAS as janelas falharem, aí sim a série é inválida.

A **lição genérica** que ficou: em integrações com APIs externas, erros raramente são binários. Um 404 pode significar "endpoint não existe", "recurso não existe" ou "recurso existe mas está vazio agora" — três situações que pedem reações completamente diferentes. O padrão "acumule resultados, decida no fim" é mais robusto que "falhe ao primeiro erro".

## 5. Agendamento via Windows Task Scheduler

Decidimos rodar a coleta **diariamente às 13:00** automaticamente, sem depender de o Vitor lembrar de executar manualmente. Para isso, criei o `agendar_windows.bat` que registra a tarefa via `schtasks /Create`.

### O erro do limite de 261 caracteres

Na primeira tentativa, o registro falhou com:

```
ERRO: O valor da opção '/TR' não pode ter mais de 261 caractere(s).
```

Limite arcaico do `schtasks`: o parâmetro `/TR` (o comando a executar) tem teto de 261 chars. Como o caminho do projeto no OneDrive da JIVE já tem 161 caracteres, e ele aparecia **duas vezes** na linha original (uma no `cd /d`, outra no redirecionamento de log), estourava.

### A solução: wrapper

Criei o arquivo `executar_coleta.bat` na própria pasta do projeto. Ele se localiza sozinho via `%~dp0` (variável especial do batch que dá o diretório do próprio script), entra na pasta e roda o Python. Daí o `/TR` precisa apontar apenas para esse wrapper — sem repetir o caminho.

```bat
@echo off
cd /d "%~dp0"
if not exist "logs" mkdir logs
python coletar_indices.py >> "logs\agendador.log" 2>&1
exit /b %ERRORLEVEL%
```

A **lição genérica**: quando você esbarra em limites obscuros de ferramentas (261 chars no `/TR`, 260 chars em paths antigos do Windows, 8191 chars na linha de comando do `cmd.exe`), a solução quase sempre é a mesma — crie um wrapper local que toma o contexto a partir da sua própria localização (`%~dp0` no batch, `__file__` em Python, `$0` em shell). Desacopla "como agendar" de "o que executar".

## 6. Fase 2 — Ingestão das Tabelas Práticas do TJSP

Concluída a base BACEN, partimos para outra fonte essencial: as **Tabelas Práticas do TJSP**, que publicam fatores acumulados de atualização monetária aplicáveis a precatórios. A primeira tabela atacada foi a da **Emenda Constitucional 113/2021**, que mudou a sistemática de correção dos débitos da Fazenda Pública (Selic isolada substituindo IPCA-E + juros).

### Descobrindo o endpoint

A descoberta foi colaborativa. O Vitor sabia que existia uma URL no TJSP com o "código 188481" para a tabela. Eu pesquisei na web e encontrei a tabela publicada em formato PDF, mas a busca não retornou referências diretas ao código 188481. O Vitor então me passou a URL completa que ele tinha:

```
https://api.tjsp.jus.br/Handlers/Handler/FileFetch.ashx?codigo=188481
```

Esse é um *handler ASP.NET* (`.ashx`) que serve arquivos do CMS do TJSP — o `188481` é o ID interno do PDF.

### Diferença fundamental em relação ao BACEN

O BACEN entrega **JSON estruturado**, atualizado diariamente. O TJSP entrega um **PDF**, atualizado mensalmente. Essa diferença mudou a abordagem em três pontos:

1. **Parsing de PDF.** Quebra a regra "stdlib only" que vinha valendo no projeto. A biblioteca `pdfplumber` foi escolhida (padrão para PDFs governamentais brasileiros, ativa, robusta).
2. **Auditoria por hash.** Em laudo pericial ou perícia contábil, quem usa a tabela precisa demonstrar **exatamente** qual versão do PDF foi consultada. Implementei versionamento por SHA-256: toda execução baixa, calcula o hash, compara com o último armazenado, e só reprocessa se mudou.
3. **Periodicidade efetiva.** Como a tabela é mensal, 95% das execuções diárias detectam "nada mudou" e param em segundos — sem reprocessar 740 fatores à toa.

### Estrutura do PDF

Layout em matriz **Ano × Mês** (12 colunas, uma por mês), com fatores acumulados desde uma base histórica em 1964 = 10.000. Particularidades:

- **Decimal brasileiro**: `1.045,540000` significa 1045,54 (ponto como milhar, vírgula como decimal — convertido para float padrão no código).
- **Células vazias**: marcadas como `::::::::::` (meses anteriores ao início da série) ou em branco (meses futuros).
- **Notas de rodapé**: conversões monetárias históricas (Cr$ → NCr$ → Cz$ → NCz$ → Cr$ → CR$ → R$) com fatores de divisão `:1000` e `:2750`. Essas notas são **críticas** para corrigir valores de períodos antigos, mas a aplicação delas é do motor de cálculo (próxima fase) — o extrator só guarda os fatores e mantém o PDF arquivado.

### Schema TJSP

Duas tabelas novas, isoladas do schema BACEN porque tratam de dados conceitualmente diferentes (índices acumulados ≠ variações mensais):

- **`tjsp_publicacoes`** — uma linha por versão de PDF baixada (id, tabela_chave, codigo_arquivo, data_download, sha256, caminho_arquivo). `UNIQUE(tabela_chave, sha256)` impede duplicar a mesma versão.
- **`tjsp_fatores`** — fatores extraídos da publicação mais recente, em formato longo (ano, mês, fator), com FK para `tjsp_publicacoes`. `UNIQUE(tabela_chave, ano, mes)` mantém uma linha por (ano, mês).

### O parser

Estratégia: extrair o texto da página com `pdfplumber.extract_text()` e parsear linha a linha com regex. Cada linha de dados começa com um ano de 4 dígitos; daí extraio os 12 primeiros tokens válidos (números BR ou marcador `::::::::::`). O ano também aparece repetido no final da linha — uma `re.sub` remove ambas as ocorrências para não atrapalhar o parsing.

Testes no PDF real validaram 740 fatores extraídos de 1964 a maio/2026, com sanity checks em valores conhecidos:

- Outubro/1964 = 10000,0 (base)
- Dezembro/2021 = 56,768468 (mês da publicação da EC 113)
- Janeiro/2024 = 63,444497
- Maio/2026 = 71,017893 (último dado)
- Junho/2026 em diante = ausente, como esperado

### Integração ao coletor

O orquestrador (`src/coletor.py`) agora tem duas funções principais:

- `coletar(modo="auto")` — só BACEN, como antes
- `coletar_tjsp()` — só TJSP
- `coletar_tudo()` — BACEN seguido de TJSP, falhas isoladas (uma fonte falhar não derruba a outra)

O `coletar_indices.py` (entry point) ganhou flags: `--so-bacen` e `--so-tjsp` permitem rodar uma fonte isoladamente; o comportamento padrão (sem flags) é a coleta completa.

## 7. Fase 3 — Ingestão das Tabelas CJF (Justiça Federal)

Com BACEN e TJSP consolidados, a terceira fonte de dados foi o **SICOM do Conselho da Justiça Federal** — o sistema que publica as Tabelas de Correção Monetária aplicáveis a processos na Justiça Federal.

### Descoberta do endpoint

A página pública (`https://sicom.cjf.jus.br/tabelaCorMor.php`) apresenta um formulário com campos dinâmicos populados via AJAX. O inspecionamento do HTML e dos arquivos JavaScript (`submeter.js`, `funcoes.js`) revelou a estrutura completa:

- O formulário faz **POST** para a própria página com os campos:
  - `acao=geraTabelaIndice` (hidden)
  - `tipoTabela`: `TCM` (Tabela de Correção Monetária) ou `VHI` (Valores Históricos dos Indexadores)
  - `seqEncadeamento`: 1 a 6 (tipo de ação judicial)
  - `mesIndice` e `anoIndice`: mês e ano de referência
  - `tipoArquivo`: `xls`
- A resposta contém um `window.location.href` com o caminho do arquivo gerado (ex.: `arquivos/temp/2026_06_tabela_de_correcao_monetaria.xls`)
- Um segundo request baixa esse arquivo

O "XLS" é, na prática, um **HTML com namespace do Excel** (MHTML) — não um binário `.xlsx`. Isso permitiu fazer o parser com regex puro, sem bibliotecas externas adicionais.

### Diferenças em relação ao TJSP

| Aspecto | TJSP | CJF |
|---------|------|-----|
| Formato | PDF binário | HTML disfarçado de XLS |
| Parser | `pdfplumber` | `re` (stdlib) |
| Periodicidade | mensal | mensal |
| Tabelas por execução | 1 | 12 (TCM×6 + VHI×6) |
| Geração | arquivo estático no CMS | arquivo gerado por POST |

### O parser

O XLS HTML tem estrutura em tabela HTML com linhas `<tr><td>` onde:
- A **primeira célula** de cada linha de dados contém exatamente o ano (ex.: `1964`)
- As **células seguintes** contêm os 12 valores mensais em decimal brasileiro

O bug inicial do parser foi o regex `^\s*(\d{4})\s` (que exige espaço após o ano) aplicado ao texto da célula — que contém exatamente `"1964"` sem espaços. A correção foi usar `^\s*(\d{4})\s*$`, que valida a célula inteira como um ano de 4 dígitos.

Após a correção, a primeira coleta extraiu **8.442 fatores** (12 tabelas × ~700 fatores cada, de 1964 a 2026).

### Idempotência e cadência mensal

A mesma estratégia de hash SHA-256 do TJSP foi aplicada: cada execução baixa o arquivo, calcula o hash e só reprocessa se mudou. Isso permite chamar a rotina diariamente sem custo extra — na prática, só haverá reprocessamento uma vez por mês, quando o CJF publicar a versão atualizada.

A rotina mensal foi agendada para o **dia 5 de cada mês** (quando o CJF costuma ter publicado os dados do mês corrente) via `agendar_cjf_mensal.bat`, separada da rotina diária do BACEN/TJSP.

### Schema CJF

Duas novas tabelas, seguindo o mesmo padrão do TJSP:

- **`cjf_publicacoes`** — um registro por download (chave, tipo, seq_encadeamento, mês/ano de referência, sha256, caminho)
- **`cjf_fatores`** — valores em formato longo, `UNIQUE(tabela_chave, ano, mes)`

### Integração ao coletor

O `coletor.py` ganhou `coletar_cjf(mes, ano)` e `coletar_tudo()` passou a incluir CJF após BACEN e TJSP. O `coletar_indices.py` ganhou os flags `--so-cjf`, `--mes` e `--ano`.

## 8. Estado atual do projeto

```
Calculadora-Claims/
├── coletar_indices.py           Entry point unificado (BACEN + TJSP + CJF)
├── executar_coleta.bat          Wrapper diário (Task Scheduler)
├── executar_coleta_cjf.bat      Wrapper mensal CJF (Task Scheduler)
├── agendar_windows.bat          Registra rotina diária no Task Scheduler
├── agendar_cjf_mensal.bat       Registra rotina mensal CJF no Task Scheduler
├── README.md                    Documentação do estado atual
├── JORNADA_DESENVOLVIMENTO.md   (este arquivo)
├── src/
│   ├── config.py                Caminhos, séries BACEN, tabelas TJSP e CJF
│   ├── bacen.py                 Cliente HTTP da API SGS (BACEN)
│   ├── tjsp.py                  Download + parser das tabelas TJSP (PDF)
│   ├── cjf.py                   Download + parser das tabelas CJF (XLS/HTML)
│   ├── db.py                    Camada SQLite (schema completo + helpers)
│   └── coletor.py               Orquestrador (BACEN + TJSP + CJF)
├── data/
│   └── claims.db                Banco SQLite com toda a base
├── referencias/
│   ├── Tabela EC113.pdf         PDF original (validação manual)
│   ├── tjsp/                    Versões arquivadas dos PDFs TJSP
│   └── cjf/                     Versões arquivadas dos XLS CJF
└── logs/
    ├── coleta_AAAA-MM-DD.log    Logs estruturados do Python
    ├── agendador.log            Saída do wrapper diário
    └── agendador_cjf.log        Saída do wrapper mensal CJF
```

**Schema SQLite** (7 tabelas):

| Tabela | Conteúdo |
|--------|----------|
| `series` | Metadados das 8 séries BACEN |
| `observacoes` | Histórico de valores BACEN (data + valor) |
| `coletas_log` | Auditoria de cada execução do coletor |
| `tjsp_publicacoes` | Histórico de PDFs baixados do TJSP (com SHA-256) |
| `tjsp_fatores` | Fatores acumulados extraídos dos PDFs TJSP |
| `cjf_publicacoes` | Histórico de XLS baixados do CJF (com SHA-256) |
| `cjf_fatores` | Coeficientes/valores extraídos das tabelas CJF |

**Dependências externas:**
- `pdfplumber` (única) — para parsing dos PDFs TJSP; módulos BACEN e CJF usam apenas stdlib

**Operação:**
- Tarefa `CalculadoraClaims_ColetaIndicesBACEN` — roda diariamente às 13:00 (BACEN + TJSP)
- Tarefa `CalculadoraClaims_ColetaCJF_Mensal` — roda todo dia 5 às 08:00 (CJF, 12 tabelas)
- Logs em `logs/coleta_AAAA-MM-DD.log`, `logs/agendador.log` e `logs/agendador_cjf.log`

## 9. Próximas fases

| # | Fase | Status |
|---|------|--------|
| 1 | Ingestão BACEN (8 séries) | ✅ Concluído |
| 2 | Ingestão TJSP (EC 113/2021) | ✅ Concluído |
| 3 | Ingestão CJF — SICOM (TCM + VHI × 6 categorias) | ✅ Concluído |
| 4 | Motor de cálculo de fatores acumulados entre datas | ⏭️ Próximo |
| 5 | Importação da planilha-protótipo | ⏭️ |
| 6 | Interface (CLI ou Streamlit) | ⏭️ |
| 7 | Definição de usuários, segurança e auditoria | ⏭️ |

A próxima fase — **motor de cálculo** — vai responder perguntas como "qual o fator de correção EC 113 entre 03/2022 e 05/2026?" ou "atualize R$ X de data Y até hoje pela Selic acumulada". É onde os dados ingeridos viram valor de negócio. Com BACEN, TJSP e CJF todos disponíveis em SQLite, o motor terá acesso a todas as fontes oficiais utilizadas na Justiça Federal e no TJSP.

Duas decisões técnicas já antecipadas para essa fase:

- **Aritmética em `Decimal`**, não `float`. Os valores armazenados em SQLite estão como `REAL` (precisão suficiente para guardar fielmente), mas qualquer multiplicação de fatores deve ler como `Decimal(str(valor))` para não acumular erro de ponto flutuante em séries longas.
- **Tratamento das conversões monetárias históricas** (Cr$/NCr$/Cz$/etc.) — implementadas no motor de cálculo, com as regras vindo do rodapé dos arquivos TJSP e CJF já arquivados.
