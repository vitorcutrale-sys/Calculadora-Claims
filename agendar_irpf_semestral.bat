@echo off
REM ============================================================
REM  Registra a coleta ANUAL da Tabela Progressiva Mensal do IRPF
REM  (Receita Federal) no Windows Task Scheduler.
REM
REM  Execute uma vez como Administrador.
REM  Roda DUAS VEZES POR ANO: 15 de janeiro e 15 de julho, as 08:00.
REM
REM  Janeiro captura a tabela do novo ano-calendario; julho captura
REM  eventuais mudancas no meio do ano (ja ocorreram em fev/2024 e
REM  maio/2025). O SHA-256 impede reprocessamento quando a pagina
REM  nao mudou, entao a segunda checagem e praticamente gratuita.
REM ============================================================

setlocal

REM Pasta onde este .bat esta -> raiz do projeto
set "PROJETO=%~dp0"
set "PROJETO=%PROJETO:~0,-1%"

REM Dia do mes e horario da execucao
set "DIA=15"
set "HORARIO=08:00"

REM Nome da tarefa no agendador
set "NOME_TAREFA=CalculadoraClaims_ColetaIRPF_Semestral"

echo.
echo Registrando tarefa "%NOME_TAREFA%"...
echo Execucao: dia %DIA% de janeiro e de julho, as %HORARIO%
echo Diretorio do projeto: %PROJETO%
echo.

schtasks /Create ^
    /SC MONTHLY ^
    /M JAN,JUL ^
    /D %DIA% ^
    /TN "%NOME_TAREFA%" ^
    /TR "\"%PROJETO%\executar_coleta_irpf.bat\"" ^
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
