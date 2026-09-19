param(
    [switch]$Smoke,
    [string]$PayloadPath = '',
    [string]$Dotnet = ''
)
$ErrorActionPreference = 'Stop'
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Dotnet) { $Dotnet = Join-Path $repositoryRoot '.tooling\dotnet\dotnet.exe' }
if (-not $PayloadPath) { $PayloadPath = Join-Path $repositoryRoot 'dist\MevoCompanion-windows-x64.zip' }
$env:DOTNET_CLI_HOME = Join-Path $repositoryRoot '.tooling\installer-dotnet-home'
$env:NUGET_PACKAGES = Join-Path $repositoryRoot '.tooling\installer-nuget'
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = '1'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
$env:DOTNET_ADD_GLOBAL_TOOLS_TO_PATH = 'false'
$project = Join-Path $PSScriptRoot 'Installer\MevoCompanionSetup.csproj'
$destination = Join-Path $repositoryRoot $(if ($Smoke) { 'artifacts\installer-smoke' } else { 'dist\installer' })
$buildArguments = @('publish', $project, '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true',
    '-p:PublishSingleFile=true', '-p:IncludeNativeLibrariesForSelfExtract=true',
    "-p:PayloadPath=$PayloadPath", '-o', $destination)
if ($Smoke) { $buildArguments += '-p:InstallerSmokeBuild=true' }
& $Dotnet @buildArguments
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
$name = if ($Smoke) { 'MevoCompanionSetup.Smoke.exe' } else { 'MevoCompanionSetup.exe' }
$result = Join-Path $destination $name
if (-not (Test-Path -LiteralPath $result)) { throw 'The installer output was not created.' }
$hash = (Get-FileHash -LiteralPath $result -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($result + '.sha256') -Value "$hash  $name" -Encoding ascii
Write-Output $result
