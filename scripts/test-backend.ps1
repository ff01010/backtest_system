$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repositoryRoot "deploy\compose.test.yml"
$projectName = "quant-backtest-tests"

Push-Location $repositoryRoot
try {
  & docker compose --project-name $projectName --file $composeFile up `
    --build --abort-on-container-exit --exit-code-from backend-tests backend-tests
  if ($LASTEXITCODE -ne 0) {
    throw "后端测试失败，退出码：$LASTEXITCODE"
  }
  & python -m pytest -q -p no:cacheprovider `
    backend/tests/test_release_manifest_generator.py
  if ($LASTEXITCODE -ne 0) {
    throw "主机外部工具测试失败，退出码：$LASTEXITCODE"
  }
} finally {
  & docker compose --project-name $projectName --file $composeFile down `
    --volumes --remove-orphans
  Pop-Location
}
