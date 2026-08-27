@echo off
REM ============================================================
REM  Wrapper chamado pelo Windows Task Scheduler (tarefa SEMESTRAL).
REM  Coleta as tabelas de referencia de baixa frequencia:
REM    - Tabela Progressiva Mensal do IRPF (Receita Federal)
REM    - Feriados bancarios (ANBIMA), usados na contagem de dias uteis
REM
REM  Ambas mudam por ato normativo (normalmente uma vez por ano, as
REM  vezes no meio do ano) - o SHA-256 garante que rodar varias vezes
REM  nao reprocessa nada quando o arquivo nao mudou.
REM ============================================================

cd /d "%~dp0"
if not exist "logs" mkdir logs
python coletar_indices.py --so-irpf     >> "logs\agendador_irpf.log" 2>&1
python coletar_indices.py --so-feriados >> "logs\agendador_irpf.log" 2>&1
exit /b %ERRORLEVEL%
