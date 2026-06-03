@echo off
REM ============================================================
REM  Registra a coleta MENSAL das tabelas CJF (Justica Federal)
REM  no Windows Task Scheduler.
REM
REM  Execute uma vez como Administrador.
REM  Roda no dia 5 de cada mes as 08:00 (o CJF costuma publicar
REM  as tabelas do mes corrente nos primeiros dias do mes).
REM ============================================================

setlocal

REM Pasta onde este .bat esta -> raiz do projeto
set "PROJETO=%~dp0"
set "PROJETO=%PROJETO:~0,-1%"

REM Dia do mes e horario da execucao
set "DIA=5"
set "HORARIO=08:00"

REM Nome da tarefa no agendador
set "NOME_TAREFA=CalculadoraClaims_ColetaCJF_Mensal"

echo.
echo Registrando tarefa "%NOME_TAREFA%"...
echo Execucao: todo dia %DIA% de cada mes as %HORARIO%
echo Diretorio do projeto: %PROJETO%
echo.

schtasks /Create ^
    /SC MONTHLY ^
    /MO 1 ^
    /D %DIA% ^
    /TN "%NOME_TAREFA%" ^
    /TR "\"%PROJETO%\executar_coleta_cjf.bat\"" ^
    /ST %HORARIO% ^
    /RL LIMITED ^
    /F

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Tarefa criada com sucesso.
    echo Para ver:        schtasks /Query /TN "%NOME_TAREFA%" /V /FO LIST
    echo Para executar:   schtasks /Run   /TN "%NOME_TAREFA%"
    echo Para remover:    schtasks /Delete /TN "%NOME_TAREFA%" /F
) else (
    echo.
    echo ERRO ao criar a tarefa. Execute este .bat como Administrador.
)

endlocal
pause
