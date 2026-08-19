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

function Get-L2RunnerReplicas {
  $line = Get-Content $envFile | Where-Object { $_ -match '^TREND_GRID_L2_RUNNER_REPLICAS=' } | Select-Object -Last 1
  if ($null -eq $line) {
    return 1
  }
  $replicas = $line.Split('=', 2)[1].Trim()
  if ($replicas -notmatch '^[1-8]$') {
    throw 'TREND_GRID_L2_RUNNER_REPLICAS in deploy/.env must be an integer from 1 to 8'
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

function Wait-ApplicationHealthy {
  param([int]$Port)

  $healthUrl = "http://127.0.0.1:$Port/api/health"
  for ($attempt = 1; $attempt -le 30; $attempt += 1) {
    try {
      $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
      if ($response.status -eq 'ok') {
        Write-Host "Local environment is ready: $healthUrl"
        return
      }
    } catch {
      # Requests can fail while containers are starting.
    }
    Start-Sleep -Seconds 2
  }
  throw "Application did not pass health check within 60 seconds: $healthUrl"
}

if (-not (Test-Path $envFile)) {
  throw "Missing local environment file: $envFile. Copy deploy/.env.example to deploy/.env first."
}

Get-Command docker -ErrorAction Stop | Out-Null
$httpPort = Get-HttpPort
$l2RunnerReplicas = Get-L2RunnerReplicas

Push-Location $projectRoot
try {
  Invoke-Compose @('up', '-d', 'postgres')
  Wait-PostgresHealthy
  Invoke-Compose @('--profile', 'migration', 'run', '--rm', 'migrate')

  $upArguments = @('up', '-d', '--scale', "l2-runner=$l2RunnerReplicas")
  if (-not $SkipBuild) {
    $upArguments += '--build'
  }
  $upArguments += 'redis', 'strategy-worker', 'bar-runner', 'l2-runner', 'backend', 'frontend'
  Invoke-Compose $upArguments
  Invoke-Compose @('ps')
  Wait-ApplicationHealthy $httpPort
} finally {
  Pop-Location
}
