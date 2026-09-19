param([switch]$Build)
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
Set-Location -LiteralPath $projectRoot
$env:DOTNET_ROOT = Join-Path $projectRoot '.tooling\dotnet'
$env:DOTNET_CLI_HOME = Join-Path $projectRoot '.tooling\dotnet-home'
$env:NUGET_PACKAGES = Join-Path $projectRoot '.tooling\nuget'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
if ($Build) {
    & (Join-Path $env:DOTNET_ROOT 'dotnet.exe') build 'desktop/MevoCompanion/MevoCompanion.csproj' -c Release --nologo --configfile 'desktop/NuGet.Config'
    if ($LASTEXITCODE -ne 0) { throw 'Native build failed' }
}
$app = Join-Path $projectRoot 'desktop\MevoCompanion\bin\Release\net10.0-windows\MevoCompanion.exe'
$cases = @(
    @{ Name = 'play'; Page = 'play'; Step = '1'; Width = '1180'; Height = '830'; Scale = '1' },
    @{ Name = 'equipment'; Page = 'setup'; Step = '1'; Width = '1180'; Height = '830'; Scale = '1' },
    @{ Name = 'reading'; Page = 'setup'; Step = '2'; Width = '1180'; Height = '830'; Scale = '1' },
    @{ Name = 'putting'; Page = 'setup'; Step = '3'; Width = '1180'; Height = '830'; Scale = '1' },
    @{ Name = 'practice'; Page = 'setup'; Step = '4'; Width = '1180'; Height = '830'; Scale = '1' },
    @{ Name = 'setup-small'; Page = 'setup'; Step = '3'; Width = '1000'; Height = '720'; Scale = '1.5' },
    @{ Name = 'connections'; Page = 'connections'; Step = '1'; Width = '1000'; Height = '720'; Scale = '1.25' },
    @{ Name = 'help'; Page = 'help'; Step = '1'; Width = '1000'; Height = '720'; Scale = '1.5' }
)
foreach ($case in $cases) {
    $report = 'desktop/artifacts/' + $case.Name + '-smoke.json'
    $argsList = @('--demo', '--data-dir', 'desktop/artifacts/demo-settings', '--smoke-test', $report,
        '--screenshot', ('desktop/artifacts/' + $case.Name + '.png'), '--page', $case.Page,
        '--step', $case.Step, '--width', $case.Width, '--height', $case.Height, '--render-scale', $case.Scale)
    $process = Start-Process -FilePath $app -ArgumentList $argsList -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(30000)) { throw ('UI smoke test is still running, process ' + $process.Id) }
    $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if (-not $result.success) { throw ($case.Name + ': ' + $result.error) }
    Write-Output ($case.Name + ': passed (' + $case.Width + 'x' + $case.Height + ', render scale ' + $case.Scale + ')')
}
