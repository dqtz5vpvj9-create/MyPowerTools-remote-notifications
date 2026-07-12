[CmdletBinding()]
param(
    [string]$MyPowerToolsRepoRoot = '',
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Release',
    [string]$OutputRoot = ''
)

$ErrorActionPreference = 'Stop'
$ToolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if ([string]::IsNullOrWhiteSpace($MyPowerToolsRepoRoot)) {
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $ToolRoot '..\..'))
    if (Test-Path -LiteralPath (Join-Path $candidate 'src\MyPowerTools.Abstractions\MyPowerTools.Abstractions.csproj')) {
        $MyPowerToolsRepoRoot = $candidate
    }
}
if ([string]::IsNullOrWhiteSpace($MyPowerToolsRepoRoot)) {
    throw 'Pass -MyPowerToolsRepoRoot with the MyPowerTools checkout path.'
}
$MyPowerToolsRepoRoot = [System.IO.Path]::GetFullPath($MyPowerToolsRepoRoot)

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $ToolRoot 'artifacts\package\android-tools-suite'
}
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$allowedRoot = [System.IO.Path]::GetFullPath((Join-Path $ToolRoot 'artifacts'))
if (-not $OutputRoot.StartsWith($allowedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputRoot must stay under $allowedRoot"
}

if (Test-Path -LiteralPath $OutputRoot) {
    Remove-Item -LiteralPath $OutputRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
Copy-Item -Path (Join-Path $ToolRoot 'current-integration\modules\android-tools-suite\*') -Destination $OutputRoot -Recurse -Force

$adapterProject = Join-Path $ToolRoot 'current-integration\src\AndroidTools.MyPowerTools\AndroidTools.MyPowerTools.csproj'
$runtimeProject = Join-Path $ToolRoot 'current-integration\src\AndroidTools.Powertoold\AndroidTools.Powertoold.csproj'
$commonProperties = @(
    '-c', $Configuration,
    "/p:MyPowerToolsRepoRoot=$MyPowerToolsRepoRoot"
)

$adapterArguments = @('build', $adapterProject) + $commonProperties + "/p:ModulePackageRoot=$OutputRoot"
& dotnet @adapterArguments
if ($LASTEXITCODE -ne 0) {
    throw "Adapter build failed with exit code $LASTEXITCODE"
}

$runtimeRoot = Join-Path $OutputRoot 'windows\x64'
$runtimeArguments = @('build', $runtimeProject) + $commonProperties + "/p:ModuleRuntimeRoot=$runtimeRoot"
& dotnet @runtimeArguments
if ($LASTEXITCODE -ne 0) {
    throw "Runtime build failed with exit code $LASTEXITCODE"
}

Write-Host $OutputRoot
