# Install ConflictScan as two Windows services (run in an elevated PowerShell).
#
#   .\service\install_services.ps1 -Python "C:\ConflictScan\.venv\Scripts\python.exe" -ServiceAccount "ROWAD\svc-conflictscan"
#
# The service account needs: read on the code folder, modify on the data folder /
# file share, and network access to the database and the model servers.
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$ServiceAccount = "",
    [System.Security.SecureString]$ServicePassword
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& $Python -m pip install pywin32 waitress | Out-Host
& $Python manage.py init | Out-Host

& $Python service\winservice.py --startup auto install | Out-Host
if ($ServiceAccount) {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($ServicePassword))
    foreach ($name in "ConflictScanWeb", "ConflictScanWorker") {
        sc.exe config $name obj= $ServiceAccount password= $plain | Out-Host
    }
}
foreach ($name in "ConflictScanWeb", "ConflictScanWorker") {
    sc.exe failure $name reset= 86400 actions= restart/10000/restart/30000/restart/60000 | Out-Host
    Start-Service $name
}
Get-Service ConflictScan* | Format-Table Name, Status, StartType
