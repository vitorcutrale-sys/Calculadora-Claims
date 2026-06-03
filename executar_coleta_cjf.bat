@echo off
REM ============================================================
REM  Wrapper chamado pelo Windows Task Scheduler (tarefa mensal CJF).
REM  Coleta as tabelas de correcao monetaria da Justica Federal.
REM ============================================================

cd /d "%~dp0"
if not exist "logs" mkdir logs
python coletar_indices.py --so-cjf >> "logs\agendador_cjf.log" 2>&1
exit /b %ERRORLEVEL%
