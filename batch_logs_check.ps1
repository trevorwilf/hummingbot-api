$p = 'E:\tradingsoftware\hummingbot-api\run_hbapi_copyforward_batch.ps1'
$e = $null
[System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$e) | Out-Null
if ($e) { $e | ForEach-Object { 'ERR: ' + $_.Message } } else { 'syntax OK' }
'non-ascii: ' + (([IO.File]::ReadAllBytes($p) | Where-Object { $_ -gt 127 }).Count)
