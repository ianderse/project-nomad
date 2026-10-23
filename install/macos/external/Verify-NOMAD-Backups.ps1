# Run on the Windows backup server. Reads backups and writes only verification reports.
param([switch]$AllBackups)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$results = @()
$folders = Get-ChildItem -LiteralPath $root -Directory | Where-Object {
    $_.Name -match '^\d{8}T\d{6}Z-' -and
    ((Test-Path -LiteralPath (Join-Path $_.FullName 'manifest.json')) -or $_.Name.EndsWith('.copying'))
} | Sort-Object Name -Descending
if (-not $AllBackups) { $folders = @($folders | Select-Object -First 1) }
foreach ($folder in $folders) {
    $deadline = (Get-Date).AddHours(2)
    $announced = $false
    do {
        $ready = $false
        $manifestFile = Join-Path $folder.FullName 'manifest.json'
        if (Test-Path -LiteralPath $manifestFile) {
            $manifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json
            if ($manifest.format -ne 1 -or -not $manifest.complete) { throw 'Unsupported or incomplete backup manifest' }
            $missing = @($manifest.sha256.PSObject.Properties | Where-Object {
                -not (Test-Path -LiteralPath (Join-Path $folder.FullName $_.Name))
            })
            $transfers = @(Get-ChildItem -LiteralPath $folder.FullName -File | Where-Object { $_.Name.EndsWith('.transferring') })
            $ready = $missing.Count -eq 0 -and $transfers.Count -eq 0
        }
        if (-not $ready) {
            if (-not $announced) { Write-Host ('Waiting for the Mac to finish copying ' + $folder.Name); $announced = $true }
            if ((Get-Date) -gt $deadline) { throw 'Copy is still incomplete after two hours. Leave the backup untouched and retry after copying resumes.' }
            Start-Sleep -Seconds 5
        }
    } until ($ready)
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
