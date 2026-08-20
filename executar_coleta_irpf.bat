@echo off
REM ============================================================
REM  Wrapper chamado pelo Windows Task Scheduler (tarefa ANUAL IRPF).
REM  Coleta a Tabela Progressiva Mensal do IRPF (Receita Federal).
REM
REM  Coleta o ano corrente. A tabela muda por lei (normalmente uma vez
REM  por ano, as vezes no meio do ano) - o SHA-256 garante que rodar
REM  varias vezes nao reprocessa nada quando a pagina nao mudou.
REM ============================================================

cd /d "%~dp0"
if not exist "logs" mkdir logs
python coletar_indices.py --so-irpf >> "logs\agendador_irpf.log" 2>&1
exit /b %ERRORLEVEL%
