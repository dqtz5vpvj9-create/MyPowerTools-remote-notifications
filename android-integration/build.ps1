[CmdletBinding()]
param(
    [string]$MyPowerToolsRepoRoot = '',
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Release'
)

# Stages the complete Remote Notifications Android module package:
#
#   artifacts/package/remote-notifications-android/
#     module.json, commands.index.json, ui/*.json      (static descriptors from package/)
#     RemoteNotifications.Android.dll                  (module adapter, net10.0)
#     BouncyCastle.Cryptography.dll                    (adapter dependency, same version as desktop)
#     ui/surface/MyPowerTools.MobileNotifications.dll  (phone surface, built from the host repository)
#
# Every required file is verified against manifest/android-package-manifest.json before the staging
# directory replaces the previous package, so a failed or partial build can never be bundled into an
# APK. Host contracts (MyPowerTools.*) and symbols are removed on purpose: the Android host resolves
# Abstractions / Platform.Abstractions / AvaloniaSdk from its own load context.

$ErrorActionPreference = 'Stop'
$IntegrationRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolRoot = Split-Path -Parent $IntegrationRoot

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
$packageId = 'remote-notifications-android'
$artifactsRoot = Join-Path $IntegrationRoot 'artifacts'
$stagingRoot = Join-Path $artifactsRoot 'package.staging'
$staging = Join-Path $stagingRoot $packageId
$package = Join-Path $artifactsRoot "package/$packageId"
$manifestPath = Join-Path $IntegrationRoot 'manifest/android-package-manifest.json'
$moduleProject = Join-Path $IntegrationRoot 'src/RemoteNotifications.Android/RemoteNotifications.Android.csproj'
$surfaceProject = Join-Path $MyPowerToolsRepoRoot 'src/MyPowerTools.MobileNotifications/MyPowerTools.MobileNotifications.csproj'
$surfaceOutput = Join-Path $MyPowerToolsRepoRoot ("artifacts/build/bin/MyPowerTools.MobileNotifications/{0}" -f $Configuration.ToLowerInvariant())

$dotnetCommand = Get-Command 'dotnet' -CommandType Application -ErrorAction SilentlyContinue
$dotnet = if ($dotnetCommand) { $dotnetCommand.Source } else { Join-Path $HOME '.dotnet/dotnet' }
if (-not (Test-Path -LiteralPath $dotnet -PathType Leaf)) {
    throw 'The dotnet host was not found. Add it to PATH, or set DOTNET_ROOT.'
}

foreach ($stale in @($stagingRoot, $package)) {
    if (Test-Path -LiteralPath $stale) {
        Remove-Item -LiteralPath $stale -Recurse -Force
    }
}

New-Item -ItemType Directory -Path (Join-Path $staging 'ui/surface') -Force | Out-Null
Copy-Item -Path (Join-Path $IntegrationRoot 'package/*') -Destination $staging -Recurse -Force

& $dotnet build $moduleProject -c $Configuration --nologo -o $staging "/p:MyPowerToolsRepoRoot=$MyPowerToolsRepoRoot"
if ($LASTEXITCODE -ne 0) {
    throw "Android module adapter build failed with exit code $LASTEXITCODE"
}

& $dotnet build $surfaceProject -c $Configuration --nologo "/p:MyPowerToolsRepoRoot=$MyPowerToolsRepoRoot"
if ($LASTEXITCODE -ne 0) {
    throw "Mobile notifications surface build failed with exit code $LASTEXITCODE"
}

$surfaceAssembly = Join-Path $surfaceOutput 'MyPowerTools.MobileNotifications.dll'
if (-not (Test-Path -LiteralPath $surfaceAssembly -PathType Leaf)) {
    throw "The mobile surface assembly was not produced at $surfaceAssembly"
}
Copy-Item -LiteralPath $surfaceAssembly -Destination (Join-Path $staging 'ui/surface') -Force

# The host provides the shared contracts; shipping another copy would break the `is IMptModule` and
# `is IMptAvaloniaSurfaceFactory` casts in the Shell. Only these exact root-level files are removed:
# ui/surface/MyPowerTools.MobileNotifications.dll is the phone surface and must survive, so no
# wildcard is used here. The manifest check below re-verifies every required file afterwards.
$sharedContracts = @(
    'MyPowerTools.Abstractions.dll',
    'MyPowerTools.Platform.Abstractions.dll',
    'MyPowerTools.AvaloniaSdk.dll'
)
foreach ($name in $sharedContracts) {
    $candidate = Join-Path $staging $name
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        Remove-Item -LiteralPath $candidate -Force
    }
}

Get-ChildItem -LiteralPath $staging -File -Filter '*.pdb' | Remove-Item -Force

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$missing = @()
foreach ($file in $manifest.files) {
    if ($file.required -and -not (Test-Path -LiteralPath (Join-Path $staging $file.path) -PathType Leaf)) {
        $missing += $file.path
    }
}

if ($missing.Count -gt 0) {
    throw ("The staged package is incomplete: " + ($missing -join ', '))
}

if (Test-Path -LiteralPath $package) {
    Remove-Item -LiteralPath $package -Recurse -Force
}

New-Item -ItemType Directory -Path (Split-Path -Parent $package) -Force | Out-Null
Move-Item -LiteralPath $staging -Destination $package
if (Test-Path -LiteralPath $stagingRoot) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}

$files = Get-ChildItem -LiteralPath $package -Recurse -File | Sort-Object FullName | ForEach-Object {
    [ordered]@{
        path = [System.IO.Path]::GetRelativePath($package, $_.FullName).Replace('\', '/')
        size = $_.Length
    }
}
$buildInfo = [ordered]@{
    schemaVersion = '1.0'
    packageId = $packageId
    configuration = $Configuration
    builtAtUtc = [DateTimeOffset]::UtcNow.ToString('O')
    files = $files
}
$buildInfo | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $artifactsRoot 'build-info.json') -Encoding utf8

Write-Output $package
