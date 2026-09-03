[CmdletBinding()]
param(
  [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $projectRoot 'deploy\.env'
$composeFile = Join-Path $projectRoot 'deploy\compose.infrastructure.yml'

function Invoke-Compose {
  param([string[]]$Arguments)

  & docker compose --env-file $envFile -f $composeFile @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose failed: $($Arguments -join ' ')"
  }
}

function Get-HttpPort {
  $line = Get-Content $envFile | Where-Object { $_ -match '^APP_HTTP_PORT=' } | Select-Object -Last 1
  if ($null -eq $line) {
    return 8080
  }
  $port = $line.Split('=', 2)[1].Trim()
  if ($port -notmatch '^\d{1,5}$') {
    throw 'APP_HTTP_PORT in deploy/.env must be a port number'
  }
  return [int]$port
}

function Get-RunnerReplicas {
  param([string]$Name)

  $line = Get-Content $envFile | Where-Object { $_ -match "^$Name=" } | Select-Object -Last 1
  if ($null -eq $line) {
    return 12
  }
  $replicas = $line.Split('=', 2)[1].Trim()
  if ($replicas -notmatch '^([1-9]|1[0-2])$') {
    throw "$Name in deploy/.env must be an integer from 1 to 12"
  }
  return [int]$replicas
}

function Wait-PostgresHealthy {
  for ($attempt = 1; $attempt -le 30; $attempt += 1) {
    $health = (& docker inspect --format '{{.State.Health.Status}}' quant-backtest-postgres 2>$null).Trim()
    if ($health -eq 'healthy') {
      return
    }
    Start-Sleep -Seconds 2
  }
  throw 'PostgreSQL did not become healthy within 60 seconds'
}

function Test-RunnerContainersReady {
  param([string]$Service, [int]$ExpectedCount)

  $containerIds = @(& docker compose --env-file $envFile -f $composeFile ps -q $Service)
  if ($containerIds.Count -ne $ExpectedCount) {
    return $false
  }
  foreach ($containerId in $containerIds) {
    $status = (& docker inspect --format '{{.State.Status}}' $containerId 2>$null).Trim()
    if ($status -ne 'running') {
      return $false
    }
  }
  return $true
}

function Wait-ApplicationHealthy {
  param(
    [int]$Port,
    [int]$BarRunnerReplicas,
    [int]$L2RunnerReplicas
  )

  $healthUrl = "http://127.0.0.1:$Port/api/health"
  for ($attempt = 1; $attempt -le 30; $attempt += 1) {
    try {
      $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
      if (
        $response.status -eq 'ok' -and
        (Test-RunnerContainersReady 'bar-runner' $BarRunnerReplicas) -and
        (Test-RunnerContainersReady 'l2-runner' $L2RunnerReplicas)
      ) {
        Write-Host "Local environment is ready: $healthUrl"
        return
      }
    } catch {
      # Requests can fail while containers are starting.
    }
    Start-Sleep -Seconds 2
  }
  Invoke-Compose @('ps')
  throw "Application or runners did not pass health check within 60 seconds: $healthUrl"
}

if (-not (Test-Path $envFile)) {
  throw "Missing local environment file: $envFile. Copy deploy/.env.example to deploy/.env first."
}

Get-Command docker -ErrorAction Stop | Out-Null
$httpPort = Get-HttpPort
$barRunnerReplicas = Get-RunnerReplicas 'TREND_GRID_BAR_RUNNER_REPLICAS'
$l2RunnerReplicas = Get-RunnerReplicas 'TREND_GRID_L2_RUNNER_REPLICAS'

Push-Location $projectRoot
try {
  Invoke-Compose @('up', '-d', 'postgres')
  Wait-PostgresHealthy
  Invoke-Compose @('--profile', 'migration', 'run', '--rm', 'migrate')

  $upArguments = @(
    'up', '-d', '--scale', "bar-runner=$barRunnerReplicas", '--scale', "l2-runner=$l2RunnerReplicas"
  )
  if (-not $SkipBuild) {
    $upArguments += '--build'
  }
  $upArguments += 'redis', 'strategy-worker', 'bar-runner', 'l2-runner', 'backend', 'frontend'
  Invoke-Compose $upArguments
  Invoke-Compose @('ps')
  Wait-ApplicationHealthy $httpPort $barRunnerReplicas $l2RunnerReplicas
} finally {
  Pop-Location
}
