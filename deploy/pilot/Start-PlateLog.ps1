#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$SettingsPath = (Join-Path $env:ProgramData 'PlateLog\config\installation.json'),
    [switch]$EnableCamera
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'setup\CertificateSetup.ps1')
$null = Get-PlateLogFullPath $SettingsPath
$settings = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
if ($settings.SchemaVersion -ne 1 -or [IO.Path]::GetFullPath($settings.PackageDirectory).TrimEnd('\') -ne [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')) {
    throw 'The installation settings do not belong to this package directory.'
}
$certificatePath = Join-Path $settings.ConfigurationDirectory 'certificate.json'
$null = Get-PlateLogFullPath $certificatePath
$certificate = Get-Content -LiteralPath $certificatePath -Raw | ConvertFrom-Json
if ($certificate.SchemaVersion -ne 1 -or $certificate.ServerName -ne $settings.SiteName) { throw 'Certificate and installation settings disagree.' }
$arguments = @('-E','-s','-B','-m','platelog','pilot','--host',$settings.ListenAddress,
    '--port',[string]$settings.StaffPort,'--allowed-host',$settings.SiteName,'--data-dir',$settings.DataDirectory,
    '--tls-cert',$certificate.CertificatePath,'--tls-key',$certificate.PrivateKeyPath)
if ($EnableCamera) { $arguments += @('--camera-transport','https','--camera-port',[string]$settings.CameraPort) }
Push-Location -LiteralPath (Join-Path $PSScriptRoot 'app')
try {
    & (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') @arguments
    if ($LASTEXITCODE -ne 0) { throw 'PlateLog exited with an error. Check the service logs or download diagnostics.' }
} finally { Pop-Location }
