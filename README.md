# Calculadora de Claims — Coleta de Índices

Projeto para construir a base de dados de índices econômicos aplicáveis a precatórios, alimentada por **três fontes oficiais** de coleta periódica, mais uma **base histórica estática** de juros de mora:

- **BACEN SGS** — séries diárias/mensais de IPCA, IGP-DI, IGP-M, Selic, CDI, TR, IPCA-E e Poupança, via API JSON.
- **TJSP — Tabelas Práticas** — PDFs oficiais com fatores acumulados de atualização monetária (atualmente, a Tabela da EC 113/2021).
- **CJF — SICOM** — Tabelas de Correção Monetária e Valores Históricos dos Indexadores do Conselho da Justiça Federal, para as 6 categorias de ação judicial, via formulário web mensal.
- **Juros de Mora (estático)** — Tabelas históricas de juros de mora em condenações da Fazenda Pública (Servidores, Não Tributário e Tributário), importadas uma única vez a partir de PDFs locais. **Sem coleta periódica**: os juros deixaram de ser aplicados em 09/12/2021 (art. 3º da EC 113/2021).
- **IRPF — Receita Federal** — Tabela Progressiva Mensal (faixa, alíquota, parcela a deduzir), coletada duas vezes por ano da página oficial, com histórico de **vigências** (a tabela muda por lei, às vezes no meio do ano). Usada no cálculo do IR-RRA.

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
│   ├── config.py                Caminhos, séries BACEN, tabelas TJSP, CJF e Juros
│   ├── bacen.py                 Cliente HTTP da API SGS (BACEN)
│   ├── tjsp.py                  Download + parser das tabelas TJSP (PDF)
│   ├── cjf.py                   Download + parser das tabelas CJF (XLS/HTML)
│   ├── juros.py                 Importador estático das tabelas de Juros de Mora (PDF)
│   ├── irpf.py                  Coleta + parser da Tabela Progressiva Mensal do IRPF
│   ├── db.py                    Camada SQLite (BACEN + TJSP + CJF + Juros)
│   ├── coletor.py               Orquestrador (BACEN + TJSP + CJF + importação de Juros)
│   └── calculo/                 Motor de cálculo (Fase 5)
│       ├── modelo.py            Premissas do ativo (inputs) + regras de datas da LOA
│       ├── perfis.py            Receitas declarativas por tipo de ativo
│       ├── indices.py           Leitores de índices em Decimal
│       ├── motor.py             Segmentador de regimes -> Valor de Face Bruto
│       └── liquido.py           Impostos, honorários e share -> Valor Líquido
├── data/
│   └── claims.db                Banco SQLite (criado na 1ª execução)
├── referencias/
│   ├── tjsp/
│   │   └── EC113_2021_AAAA-MM-DD_<hash>.pdf   Versões arquivadas dos PDFs TJSP
│   ├── cjf/
│   │   └── TCM_3_AAAA-MM-DD_<hash>.xls        Versões arquivadas dos XLS CJF
│   └── juros/
│       └── JUROS_SERVIDOR_AAAA-MM-DD_<hash>.pdf  Cópias arquivadas dos PDFs de Juros
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
| 29543  | Taxa Legal               | % a.m.    | BCB   | mensal        |

> **Taxa Legal (29543)** — taxa legal de juros do art. 406 do Código Civil (redação da Lei 14.905/2024 e Resolução CMN 5.171/2024): Selic deduzido o IPCA. Publicada desde ago/2024; usada como juros do regime pós-EC 136 para precatórios da LOA 2027 em diante.

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

## Tabelas de Juros de Mora (base histórica estática)

Tabelas de **juros de mora em condenações da Fazenda Pública** (precatórios), importadas a partir de PDFs locais. São dados **históricos e fechados**: os juros de mora deixaram de ser aplicados a partir de **09/12/2021** (art. 3º da EC 113/2021), então **não há coleta periódica** — a importação é feita uma única vez.

| Chave                  | Categoria                                            | Arquivo de origem       |
|------------------------|------------------------------------------------------|-------------------------|
| `JUROS_SERVIDOR`       | Servidores e Empregados Públicos                     | `TaxaJuroServidor.pdf`  |
| `JUROS_NAO_TRIBUTARIO` | Precatórios de Natureza Não Tributária               | `TaxaJuroGeral.pdf`     |
| `JUROS_TRIBUTARIO`     | Precatórios Tributários                              | `TaxaJuroTributario.pdf`|

Cada PDF é uma matriz **Ano × Mês** com a taxa de juros de mora **acumulada (em %)** daquele mês até a data de referência (dez/2021). Os valores embutem as mudanças de regime legal ao longo do tempo (0,5% a.m. do Código Civil de 1916, 1% a.m. do DL 2.322/1987 e da Lei 8.177/1991, poupança via Lei 11.960/2009 e EC 62/2009, etc.) — por isso são **extraídos fielmente, nunca recalculados**.

O parser usa `pdfplumber.extract_tables()`, que preserva o **alinhamento por coluna** (essencial porque o ano inicial, 1964, é parcial: só tem out/nov/dez). Cada PDF é arquivado em `referencias/juros/` com SHA-256 no nome; reimportar uma versão idêntica não reprocessa nada.

Para importar:

```cmd
python coletar_indices.py --importar-juros                          # origem padrão: referencias/juros/
python coletar_indices.py --importar-juros --origem "C:\caminho\dos\pdfs"   # origem custom
```

Para adicionar ou remover tabelas, edite o dicionário `JUROS_TABELAS` em `src/config.py`.

## Tabela Progressiva Mensal do IRPF

Coletada da página oficial da Receita Federal (uma por ano-calendário), da qual extraímos **apenas a Tabela de Incidência Mensal** — a que o IR-RRA usa.

```cmd
python coletar_indices.py --so-irpf              # ano corrente
python coletar_indices.py --so-irpf --ano 2025   # ano específico
```

**Vigências, não anos.** A tabela muda por lei e às vezes no meio do ano (houve mudança em fev/2024 e maio/2025), então cada versão é gravada com `vigencia_inicio`/`vigencia_fim`. Uma mesma página pode conter mais de uma vigência. A consulta `db.tabela_irpf_vigente(conn, data)` devolve a tabela aplicável a qualquer data.

**Validação estrita.** A página não usa `<table>` HTML — é texto estruturado. O parser ancora no cabeçalho `Base de cálculo / Alíquota / Dedução`, distingue a tabela mensal da anual pela magnitude e **exige** 5 faixas com alíquotas 0 / 7,5 / 15 / 22,5 / 27,5 e limites crescentes. Se a página mudar de formato ou a lei alterar as alíquotas, a coleta **falha com erro claro** apontando o HTML arquivado em `referencias/irpf/`, em vez de gravar número errado.

A cadência é semestral (ver agendamento abaixo) — o SHA-256 garante que rodar de novo não reprocessa nada quando a página não mudou.

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
python coletar_indices.py --importar-juros     # importa as tabelas de Juros de Mora (estático)
python coletar_indices.py --so-irpf            # coleta a Tabela Progressiva Mensal do IRPF
```

> **Nota:** `--importar-juros` é uma operação estática e **não** faz parte da coleta padrão (`coletar_tudo`). Rode-a manualmente apenas quando precisar (re)importar os PDFs de juros.

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

### Rotina semestral — IRPF

1. Clique com o botão direito em `agendar_irpf_semestral.bat`.
2. Escolha **Executar como administrador**.
3. Registra a tarefa `CalculadoraClaims_ColetaIRPF_Semestral` para rodar nos dias **15 de janeiro e 15 de julho às 08:00** — janeiro captura a tabela do novo ano-calendário; julho captura eventuais mudanças no meio do ano.

```cmd
schtasks /Query  /TN "CalculadoraClaims_ColetaIRPF_Semestral" /V /FO LIST
schtasks /Run    /TN "CalculadoraClaims_ColetaIRPF_Semestral"
schtasks /Delete /TN "CalculadoraClaims_ColetaIRPF_Semestral" /F
```

Os logs ficam em `logs/agendador.log` (wrapper diário), `logs/agendador_cjf.log` (wrapper mensal), `logs/agendador_irpf.log` (wrapper semestral) e `logs/coleta_AAAA-MM-DD.log` (logs estruturados do Python).

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

**Tabela `juros_publicacoes`** — versões dos PDFs de Juros de Mora importados

| coluna          | tipo    |
|-----------------|---------|
| id (PK)         | INTEGER |
| tabela_chave    | TEXT (ex.: "JUROS_SERVIDOR") |
| descricao       | TEXT    |
| data_referencia | TEXT ("YYYY-MM": acumulado até) |
| data_importacao | TEXT    |
| sha256          | TEXT    |
| caminho_arquivo | TEXT (path em referencias/juros/) |

`UNIQUE (tabela_chave, sha256)` — mesma versão não duplica.

**Tabela `juros_fatores`** — taxas de juros de mora acumuladas (em %)

| coluna         | tipo    |
|----------------|---------|
| id (PK)        | INTEGER |
| tabela_chave   | TEXT    |
| ano            | INTEGER |
| mes            | INTEGER (1..12) |
| taxa           | REAL (% de juros de mora acumulado) |
| publicacao_id  | INTEGER → juros_publicacoes.id |
| atualizado_em  | TEXT    |

`UNIQUE (tabela_chave, ano, mes)` — uma linha por mês/ano em cada tabela.

**Tabela `irpf_publicacoes`** — versões da página do IRPF coletadas

| coluna | tipo |
|--------|------|
| id (PK) | INTEGER |
| ano_pagina | INTEGER (ano-calendário da página) |
| fonte_url | TEXT |
| data_coleta | TEXT |
| sha256 | TEXT |
| caminho_arquivo | TEXT (path em referencias/irpf/) |

`UNIQUE (ano_pagina, sha256)` — mesma versão não duplica.

**Tabela `irpf_faixas`** — faixas da tabela progressiva mensal, por vigência

| coluna | tipo |
|--------|------|
| id (PK) | INTEGER |
| vigencia_inicio | TEXT (YYYY-MM-DD) |
| vigencia_fim | TEXT ou NULL (vigente) |
| ordem | INTEGER (1..5) |
| base_de | REAL |
| base_ate | REAL ou NULL (última faixa) |
| aliquota | REAL (pontos percentuais, ex.: 7.5) |
| deducao | REAL (parcela a deduzir, R$) |
| publicacao_id | INTEGER → irpf_publicacoes.id |

`UNIQUE (vigencia_inicio, ordem)` — uma linha por faixa em cada vigência.

## Motor de cálculo (Fase 5)

O pacote `src/calculo/` calcula o **Valor de Face Bruto** de um precatório federal, reconstruindo o saldo ao longo do tempo a partir dos índices já ingeridos.

### Como usar

```python
from datetime import date
from decimal import Decimal
from src import db
from src.calculo.modelo import PremissasAtivo
from src.calculo import motor

premissas = PremissasAtivo(
    valor_principal=Decimal("65953153"),
    valor_juros=Decimal("234154969"),
    data_base=date(2015, 7, 1),
    ano_orcamento=2016,               # define a expedição e o fim da LOA
    perfil="federal_servidores",
)
with db.conectar() as conn:
    r = motor.calcular_vf_bruto(conn, premissas)   # default: até hoje

print(r.saldo)        # Valor de Face Bruto
print(r.principal, r.juros, r.selic)               # componentes
for seg in r.trace:   # auditoria: um registro por segmento de regime
    print(seg["regime"], seg["de"], seg["ate"], seg["saldo"])
```

### Regimes encadeados

O motor segmenta a linha do tempo entre a data base e a data de cálculo, aplicando o regime vigente em cada trecho:

| Regime | Janela | Método |
|--------|--------|--------|
| **R1** | até nov/2021 | correção SICOM/CJF (fecha na referência dez/2021) + juros de mora |
| **R2** | dez/2021 a jul/2025 | Selic **linear** sobre o saldo global (EC 113/2021) |
| **R3** | ago/2025 em diante | IPCA composto + juros: **2% a.a. pro-rata** (DAYS360) para LOA ≤ 2026, ou **Taxa Legal** (série 29543) para LOA ≥ 2027 |
| **R_graça** | expedição+1 até 31/12 do ano da LOA | IPCA-E, **sem juros** — *sobrepõe* o regime legal na sua janela |

O `R_graça` recorta a régua legal: um precatório antigo pode passar por `R1 → R_graça → R1 → R2 → R3`. A composição principal/juros/selic é carregada por toda a cadeia (o juros do R3 incide só sobre o principal corrigido).

Datas derivadas do **ano orçamentário** (`src/calculo/modelo.py`):

| Ano orçamento | Expedição (fim da correção pré-graça) | Fim da LOA |
|---------------|----------------------------------------|-----------|
| ≤ 2022        | 30/06 do ano anterior                  | 31/12 do ano |
| 2023–2026     | 31/03 do ano anterior                  | 31/12 do ano |
| ≥ 2027        | 31/01 do ano anterior                  | 31/12 do ano |

### Perfis disponíveis

| Perfil | Categoria (CJF) | Correção R1 | Juros de mora |
|--------|-----------------|-------------|---------------|
| `federal_desap` | 1 — Desapropriações | TCM_1 | Geral + **compensatórios** |
| `federal_previdenciario` | 2 — Benefício Previdenciário | TCM_2 | Geral |
| `federal_servidores` | 3 — Servidores | TCM_3 | Servidor |
| `federal_tributario` | 4 — Indébito Tributário | TCM_4 | Tributário |
| `federal_fazenda_geral` | 5 — Devedor Fazenda Pública | TCM_5 | Geral |
| `federal_naofazenda_geral` | 6 — Devedor não-Fazenda | TCM_6 | Geral |

**`federal_tributario` tem regime próprio** (não usa graça nem os cortes EC 113/EC 136): correção TCM_4 + juros de mora até 01/1996 e, a partir daí, **Selic linear somente sobre o principal** (o componente de juros fica fixo).

**`federal_desap`** aceita duas booleanas em `PremissasAtivo`:
- `precatorio_expedido` — quando `False`, incidem **juros compensatórios simples** sobre o principal corrigido, dentro do R1.
- `transito_antes_adi2332` — `True`: 1% a.m. em todo o R1; `False`: 1% a.m. até 10/06/1997 e 0,5% a.m. até nov/2021 (de dez/2021 em diante já estão embutidos na Selic / Taxa Legal).

Para acrescentar um tipo de ativo, adicione um perfil em `src/calculo/perfis.py` — o motor é genérico e não precisa ser alterado.

### Valor de Face Líquido

`src/calculo/liquido.py` parte do VF Bruto e aplica os descontos:

```python
from src.calculo import liquido

with db.conectar() as conn:
    bruto = motor.calcular_vf_bruto(conn, premissas)
    r = liquido.calcular_vf_liquido(conn, bruto, premissas)

r.vf_bruto        # montante global (principal + juros + selic)
r.impostos        # PSS + IR-RRA + IRRF
r.vf_liquido      # VF Bruto - impostos (sem honorários)
r.total_cedivel   # VF Líquido - honorários
r.total_share     # Total Cedível × percentual do ativo
r.descontos       # auditoria: base, alíquota e valor de cada desconto
```

A cascata:

```
VF BRUTO
(-) impostos          duas trilhas mutuamente exclusivas (ver abaixo)
(=) VF LÍQUIDO
(-) honorários        base: bruto se destacados, líquido se não
(=) TOTAL CEDÍVEL
(×) share do ativo
```

**Duas trilhas tributárias, definidas pelo tipo de verba:**

| Perfil | Impostos aplicados |
|--------|--------------------|
| `federal_servidores` (verba remuneratória) | **PSS** (% sobre o principal corrigido) + **IR-RRA** |
| Todos os demais | **IRRF de 3%** sobre o montante global (art. 27 da Lei 10.833/2003, sem deduções) |

- **PSS** — alíquota informada caso a caso, via `pss_percentual` (fração) **ou** `pss_valor` (R$ da conta na data base, do qual a alíquota é derivada). Incide só sobre o principal.
- **IR-RRA** — `nº_meses × (base_mensal × alíquota − dedução)`, com `base_mensal = (principal − PSS) / nº_meses`, usando a tabela do IRPF vigente. Os juros não entram na base. A isenção da Lei 15.191/2025 não se aplica (rendimento recebido acumuladamente).
- **IRRF 3%** — `aliquota_imposto` permite sobrepor a alíquota legal quando necessário.
- **Honorários** — `honorarios_percentual`, com a base definida por `destacamento_honorarios`.

### Precisão

Todo o motor opera em `Decimal` (nunca `float`), lendo os valores do banco como `Decimal(str(valor))`, para não acumular erro de ponto flutuante nas multiplicações encadeadas de fatores.

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
4. ✅ Importação estática das Tabelas de Juros de Mora (3 categorias, base histórica)
5. ✅ Ingestão semestral da Tabela Progressiva Mensal do IRPF (com histórico de vigências)
6. 🔄 Motor de cálculo — Valor de Face **Bruto** e **Líquido** dos 6 tipos de precatório
   federal (concluídos); pendentes: curva mensal e persistência das curvas
7. ⏭️ Interface (CLI ou Streamlit) para rodar cálculos
8. ⏭️ Precatórios estaduais (servidores, desapropriação, tributário, genéricos)
9. ⏭️ Definir usuários finais e segurança/auditoria