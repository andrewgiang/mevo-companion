param([Parameter(Mandatory=$true)][string]$Source)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$installRoot = Join-Path $env:LOCALAPPDATA 'Programs\MevoCompanion'
$readerPath = Join-Path $installRoot 'reader\FsGolfReader.exe'
$enginePath = Join-Path $installRoot 'engine\MevoCompanionEngine.exe'
$manifest = Get-Content -LiteralPath (Join-Path $installRoot '.mevo-companion-install.json') -Raw | ConvertFrom-Json
if ($manifest.Application -ne 'org.mevocompanion.windows') { throw 'Not a managed companion installation.' }
$sourcePath = (Resolve-Path -LiteralPath $Source).Path
if (-not $sourcePath.StartsWith($repoRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Source must be a built reader in this workspace.' }
if (-not (Test-Path -LiteralPath $readerPath -PathType Leaf)) { throw 'Installed reader is missing.' }
$stage = Join-Path $repoRoot ('artifacts\reader-update-' + [guid]::NewGuid().ToString('N') + '.exe')
$backup = Join-Path $repoRoot ('artifacts\reader-backup-' + [guid]::NewGuid().ToString('N') + '.exe')
Copy-Item -LiteralPath $sourcePath -Destination $stage
# Validate the isolated executable before touching the running reader. A thin
# .NET app host can pass its build checks but still require adjacent DLL files.
$checkInfo = [Diagnostics.ProcessStartInfo]::new($stage, '--self-test')
$checkInfo.UseShellExecute = $false
$checkInfo.CreateNoWindow = $true
$checkInfo.RedirectStandardOutput = $true
$checkInfo.RedirectStandardError = $true
$check = [Diagnostics.Process]::Start($checkInfo)
$checkOutput = $check.StandardOutput.ReadToEndAsync()
$checkError = $check.StandardError.ReadToEndAsync()
if (-not $check.WaitForExit(15000)) {
    $check.Kill()
    throw 'Isolated reader self-check timed out; installed reader was not changed.'
}
if ($check.ExitCode -ne 0) {
    throw ('Isolated reader self-check failed; installed reader was not changed. ' + $checkError.Result + $checkOutput.Result)
}
$owned = @(Get-CimInstance Win32_Process -Filter "Name='FsGolfReader.exe'" | Where-Object { $_.ExecutablePath -eq $readerPath })
foreach ($reader in $owned) {
    $parent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $reader.ParentProcessId)
    if ($parent.ExecutablePath -ne $enginePath) { throw 'Reader is not owned by the installed companion engine.' }
}
foreach ($reader in $owned) {
    $process = Get-Process -Id $reader.ProcessId -ErrorAction Stop
    if ($process.Path -ne $readerPath) { throw 'Reader process identity changed.' }
    $process.Kill()
    if (-not $process.WaitForExit(2000)) { throw 'Owned reader did not exit.' }
}
# Atomic replacement keeps the existing reader intact if the update fails.
# The owner restarts this helper and establishes a new shot baseline.
for ($attempt = 0; ; $attempt++) {
    try {
        [IO.File]::Replace($stage, $readerPath, $backup)
        break
    } catch [IO.IOException] {
        # A failed helper can briefly reopen while its owner is reconnecting.
        # Atomic replacement is safe to retry only while the stage still exists.
        if ($attempt -ge 19 -or -not (Test-Path -LiteralPath $stage -PathType Leaf)) { throw }
        Start-Sleep -Milliseconds 100
    }
}
if ((Get-FileHash -LiteralPath $sourcePath).Hash -ne (Get-FileHash -LiteralPath $readerPath).Hash) { throw 'Installed reader hash did not match.' }
[pscustomobject]@{Updated=$readerPath; Backup=$backup; ReplacedProcesses=$owned.ProcessId}
