@echo off
REM ============================================================
REM  Rotina diaria (agendada para as 03:00 por agendar_rotinas.ps1)
REM
REM  1. Coleta todos os indices: BACEN, poupanca, TJSP e CJF.
REM  2. Gera a calculadora em arquivo unico com o banco atualizado.
REM
REM  A calculadora e gerada mesmo que alguma fonte falhe na coleta:
REM  vale o que foi coletado, e o erro fica registrado no log.
REM ============================================================
setlocal

REM Reabre a si mesmo minimizado: uma janela de console aberta na tela
REM pode ser fechada por engano e interrompe a rotina no meio.
if /i not "%~1"=="--rodando" (
    start "Calculadora Claims - rotina diaria" /min cmd /c ""%~f0" --rodando"
    exit /b 0
)

REM Pasta onde a calculadora deve ser salva (ex.: pasta do SharePoint
REM sincronizada no OneDrive). Em branco, vai para dist\ do projeto.
set "SAIDA="
set "ARQUIVO=Calculadora de Precatorios.html"

REM Saida do Python em UTF-8 no log (acentos e simbolos das mensagens).
set "PYTHONUTF8=1"

cd /d "%~dp0"
if not exist "logs" mkdir logs
set "LOG=logs\rotina_diaria.log"

echo. >> "%LOG%"
echo ===== %date% %time% ===== >> "%LOG%"

python coletar_indices.py >> "%LOG%" 2>&1
set "ERRO_COLETA=%ERRORLEVEL%"

if defined SAIDA (
    python gerar_html.py --saida "%SAIDA%\%ARQUIVO%" >> "%LOG%" 2>&1
) else (
    python gerar_html.py >> "%LOG%" 2>&1
)
set "ERRO_HTML=%ERRORLEVEL%"

echo Coleta: %ERRO_COLETA%  Calculadora: %ERRO_HTML% >> "%LOG%"
if not "%ERRO_HTML%"=="0" exit /b 2
if not "%ERRO_COLETA%"=="0" exit /b 1
exit /b 0
