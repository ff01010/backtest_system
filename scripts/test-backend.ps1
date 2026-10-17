$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repositoryRoot "deploy\compose.test.yml"
$projectName = "quant-backtest-tests"

function Invoke-ComposeTestPhase {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Service,
    [switch]$Build
  )

  $arguments = @(
    "compose", "--project-name", $projectName, "--file", $composeFile,
    "up", "--abort-on-container-exit", "--exit-code-from", $Service
  )
  if ($Build) {
    $arguments += "--build"
  }
  $arguments += $Service
  & docker @arguments
  if ($LASTEXITCODE -ne 0) {
    throw "$Service 测试失败，退出码：$LASTEXITCODE"
  }
}

function Remove-TestEnvironment {
  param(
    [switch]$BestEffort
  )

  & docker compose --project-name $projectName --file $composeFile down `
    --volumes --remove-orphans
  if ($LASTEXITCODE -ne 0 -and -not $BestEffort) {
    throw "测试环境清理失败，退出码：$LASTEXITCODE"
  }
}

Push-Location $repositoryRoot
try {
  Invoke-ComposeTestPhase -Service "migration-upgrade-test"
  Remove-TestEnvironment
  Invoke-ComposeTestPhase -Service "backend-tests" -Build
  & python -m pytest -q -p no:cacheprovider `
    backend/tests/test_release_manifest_generator.py
  if ($LASTEXITCODE -ne 0) {
    throw "主机外部工具测试失败，退出码：$LASTEXITCODE"
  }
} finally {
  try {
    Remove-TestEnvironment -BestEffort
  } finally {
    Pop-Location
  }
}
