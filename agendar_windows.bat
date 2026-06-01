@echo off
REM ============================================================
REM  Registra a coleta diária no Windows Task Scheduler.
REM  Execute uma vez (clique direito -> "Executar como administrador").
REM  Depois disso o Windows roda o script todo dia automaticamente.
REM ============================================================

setlocal

REM Pasta onde este .bat está -> raiz do projeto
set "PROJETO=%~dp0"
set "PROJETO=%PROJETO:~0,-1%"

REM Horario diario (HH:MM, formato 24h). Ajuste se desejar.
set "HORARIO=13:00"

REM Nome da tarefa no agendador
set "NOME_TAREFA=CalculadoraClaims_ColetaIndicesBACEN"

echo.
echo Registrando tarefa "%NOME_TAREFA%" para rodar diariamente as %HORARIO%...
echo Diretorio do projeto: %PROJETO%
echo.

REM /TR aponta para o wrapper executar_coleta.bat na propria pasta do projeto.
REM O wrapper se localiza via %~dp0, evitando o limite de 261 caracteres do /TR
REM quando o caminho do projeto e longo (OneDrive, nomes com espacos, etc.).

schtasks /Create ^
    /SC DAILY ^
    /TN "%NOME_TAREFA%" ^
    /TR "\"%PROJETO%\executar_coleta.bat\"" ^
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
