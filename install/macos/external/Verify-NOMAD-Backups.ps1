# Run on the Windows backup server. Reads backups and writes only verification reports.
param([switch]$AllBackups)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$results = @()
$folders = Get-ChildItem -LiteralPath $root -Directory | Where-Object {
    (Test-Path -LiteralPath (Join-Path $_.FullName 'manifest.json')) -and
    (Test-Path -LiteralPath (Join-Path $_.FullName 'files.tar'))
} | Sort-Object Name -Descending
if (-not $AllBackups) { $folders = @($folders | Select-Object -First 1) }
foreach ($folder in $folders) {
    $manifest = Get-Content -LiteralPath (Join-Path $folder.FullName 'manifest.json') -Raw | ConvertFrom-Json
    foreach ($entry in $manifest.sha256.PSObject.Properties) {
        if ([System.IO.Path]::GetFileName($entry.Name) -ne $entry.Name) { throw 'Unsafe manifest path' }
        $file = Join-Path $folder.FullName $entry.Name
        Write-Host ('Checking ' + $folder.Name + '\' + $entry.Name)
        $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        $results += [PSCustomObject]@{
            Backup = $folder.Name; File = $entry.Name; Expected = $entry.Value
            Actual = $actual; MatchesExpected = ($actual -eq $entry.Value)
            Bytes = (Get-Item -LiteralPath $file).Length
            CheckedAt = (Get-Date).ToUniversalTime().ToString('o')
        }
        $results | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $root 'NOMAD-backup-hash-results.json') -Encoding UTF8
        $results | Format-Table -AutoSize | Out-String -Width 240 | Set-Content -LiteralPath (Join-Path $root 'NOMAD-backup-hash-results.txt')
        Write-Host ('  Matches: ' + $results[-1].MatchesExpected)
    }
}
Write-Host 'Finished. Results are saved beside this script.'
