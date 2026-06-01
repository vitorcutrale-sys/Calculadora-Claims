@echo off
REM ============================================================
REM  Wrapper chamado pelo Windows Task Scheduler.
REM  Existe para contornar o limite de 261 caracteres do /TR.
REM
REM  Resolve sua propria pasta via %~dp0 (que ja tem a barra
REM  final), entra nela, garante a existencia de logs/, e roda
REM  o coletor capturando stdout+stderr em logs/agendador.log.
REM ============================================================

cd /d "%~dp0"
if not exist "logs" mkdir logs
python coletar_indices.py >> "logs\agendador.log" 2>&1
exit /b %ERRORLEVEL%
