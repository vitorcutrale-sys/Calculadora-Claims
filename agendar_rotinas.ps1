<#
Registra as rotinas automaticas da Calculadora de Claims no Agendador de
Tarefas do Windows. Idempotente: pode ser rodado de novo para recriar.

  CalculadoraClaims_RotinaDiaria          todo dia 03:00
      executar_rotina_diaria.bat -> coleta (BACEN, poupanca, TJSP, CJF)
                                    + gera a calculadora em arquivo unico
  CalculadoraClaims_ColetaIRPF_Semestral  dia 15 de jan e jul, 03:30
      executar_coleta_irpf.bat   -> tabela do IRPF + feriados ANBIMA

Configuracao pensada para notebook:
  - acorda o computador para rodar (se estiver em suspensao);
  - se o horario foi perdido (computador desligado), roda assim que possivel;
  - roda tambem na bateria.
Para acordar o computador, o Windows precisa permitir "timers de ativacao"
(Opcoes de energia > Suspender > Permitir temporizadores de ativacao).

Remove as tarefas antigas que esta rotina substitui.

Uso (PowerShell, na pasta do projeto):
    powershell -ExecutionPolicy Bypass -File .\agendar_rotinas.ps1
#>

$ErrorActionPreference = "Stop"
$projeto = $PSScriptRoot

$configuracoes = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

function Registrar($nome, $bat, $criarComSchtasks) {
    # schtasks cobre gatilhos mensais por mes do ano, que o New-ScheduledTaskTrigger
    # nao oferece; acao e configuracoes de energia entram depois, via Set-ScheduledTask.
    & schtasks.exe /Create /TN $nome /TR "`"$projeto\$bat`"" /RL LIMITED /F @criarComSchtasks | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Falha ao criar a tarefa $nome." }
    # cmd direto: o PowerShell classico e o modo "sem usuario conectado" sao
    # bloqueados pela politica desta maquina. O .bat se reabre minimizado.
    $acao = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c `"`"$projeto\$bat`"`"" -WorkingDirectory $projeto
    Set-ScheduledTask -TaskName $nome -Action $acao -Settings $configuracoes | Out-Null
    $info = Get-ScheduledTask -TaskName $nome | Get-ScheduledTaskInfo
    Write-Host ("  {0,-42} proxima execucao: {1}" -f $nome, $info.NextRunTime)
}

Write-Host "Registrando rotinas em $projeto"
Registrar "CalculadoraClaims_RotinaDiaria" "executar_rotina_diaria.bat" @("/SC", "DAILY", "/ST", "03:00")
Registrar "CalculadoraClaims_ColetaIRPF_Semestral" "executar_coleta_irpf.bat" @("/SC", "MONTHLY", "/M", "JAN,JUL", "/D", "15", "/ST", "03:30")

# Substituidas: a coleta diaria do BACEN e a mensal do CJF agora fazem parte
# da rotina diaria.
foreach ($antiga in "CalculadoraClaims_ColetaIndicesBACEN", "CalculadoraClaims_ColetaCJF_Mensal") {
    if (Get-ScheduledTask -TaskName $antiga -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $antiga -Confirm:$false
        Write-Host "  removida tarefa antiga: $antiga"
    }
}
