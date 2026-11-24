$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$backendGate = Join-Path $PSScriptRoot "test-backend.ps1"

function Assert-LastCommandSucceeded {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Operation
  )

  if ($LASTEXITCODE -ne 0) {
    throw "$Operation 失败，退出码：$LASTEXITCODE"
  }
}

Push-Location $repositoryRoot
try {
  $branch = (& git branch --show-current).Trim()
  Assert-LastCommandSucceeded -Operation "读取当前分支"
  if ($branch -ne "develop") {
    throw "本地发布检查只能从 develop 执行，当前分支：$branch"
  }

  $workingTree = & git status --porcelain
  Assert-LastCommandSucceeded -Operation "检查工作区"
  if ($workingTree) {
    throw "工作区有未提交改动，请提交或处理后再运行发布检查"
  }

  Write-Host "[1/4] 获取远程 main 与 develop 状态"
  & git fetch origin main develop
  Assert-LastCommandSucceeded -Operation "获取远程分支"

  $divergence = (& git rev-list --left-right --count origin/develop...HEAD).Trim()
  Assert-LastCommandSucceeded -Operation "比较本地与远程 develop"
  $counts = $divergence -split "\s+"
  $behind = [int]$counts[0]
  $ahead = [int]$counts[1]
  if ($behind -gt 0) {
    throw "本地 develop 落后 origin/develop $behind 个提交，请先同步"
  }

  $pendingCommits = & git log --oneline origin/main..HEAD
  Assert-LastCommandSucceeded -Operation "读取待发布提交"
  if (-not $pendingCommits) {
    throw "develop 相对 origin/main 没有待发布提交"
  }

  $candidateHead = (& git rev-parse HEAD).Trim()
  Assert-LastCommandSucceeded -Operation "读取候选提交"
  Write-Host "[2/4] 发布候选：$candidateHead"
  Write-Host "待合入 main 的提交："
  $pendingCommits | ForEach-Object { Write-Host "  $_" }
  Write-Host "本地 develop 领先 origin/develop：$ahead 个提交"

  Write-Host "[3/4] 运行完整本地门禁"
  & $backendGate
  if ($LASTEXITCODE -ne 0) {
    throw "完整本地门禁失败，退出码：$LASTEXITCODE"
  }

  $verifiedHead = (& git rev-parse HEAD).Trim()
  Assert-LastCommandSucceeded -Operation "复核候选提交"
  if ($verifiedHead -ne $candidateHead) {
    throw "测试期间 HEAD 已变化，请重新执行发布检查"
  }
  $finalWorkingTree = & git status --porcelain
  Assert-LastCommandSucceeded -Operation "复核工作区"
  if ($finalWorkingTree) {
    throw "测试后工作区出现改动，请处理后重新执行发布检查"
  }

  Write-Host "[4/4] 本地发布检查通过"
  Write-Host "候选提交：$candidateHead"
  Write-Host "下一步：推送 develop，并在 GitLab 将 develop 合入 main。"
} finally {
  Pop-Location
}
