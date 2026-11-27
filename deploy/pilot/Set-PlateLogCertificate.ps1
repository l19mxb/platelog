#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$SettingsPath = (Join-Path $env:ProgramData 'PlateLog\config\installation.json'),
    [ValidateSet('ITManaged','PlateLogGenerated')][string]$CertificateMode,
    [ValidateRange(1,730)][int]$CertificateDays = 365,
    [string[]]$AdditionalCertificateName = @(),
    [string]$CertificatePath,
    [string]$PrivateKeyPath,
    [string]$TrustBundlePath,
    [switch]$NonInteractive
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'setup\CertificateSetup.ps1')
$null = Get-PlateLogFullPath $SettingsPath
$settings = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
if ($settings.SchemaVersion -ne 1 -or [IO.Path]::GetFullPath($settings.PackageDirectory).TrimEnd('\') -ne [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')) {
    throw 'The installation settings do not belong to this package directory.'
}
$currentPath = Join-Path $settings.ConfigurationDirectory 'certificate.json'
$null = Get-PlateLogFullPath $currentPath
$current = Get-Content -LiteralPath $currentPath -Raw | ConvertFrom-Json
if (!$PSBoundParameters.ContainsKey('AdditionalCertificateName')) {
    $AdditionalCertificateName = @($current.Names | Where-Object { $_ -ne $settings.SiteName })
}
$arguments = @{ConfigurationDirectory=$settings.ConfigurationDirectory;PythonPath=(Join-Path $PSScriptRoot '.venv\Scripts\python.exe');
    Validator=(Join-Path $PSScriptRoot 'setup\check_certificate.py');ServerName=$settings.SiteName;
    RuntimeSid=(Get-PlateLogRuntimeSid $settings.RuntimeSid);Mode=$CertificateMode;AdditionalName=$AdditionalCertificateName;
    CertificatePath=$CertificatePath;PrivateKeyPath=$PrivateKeyPath;TrustBundlePath=$TrustBundlePath;
    DisallowedDirectory=@($PSScriptRoot,$settings.DataDirectory);Replace=$true;NonInteractive=$NonInteractive}
if ($current.LifetimeDays -ge 1 -and $current.LifetimeDays -le 730) { $arguments.DefaultDays = [int]$current.LifetimeDays }
if ($PSBoundParameters.ContainsKey('CertificateDays')) { $arguments.Days = $CertificateDays }
$result = Set-PlateLogCertificateConfiguration @arguments
Write-Host 'Certificate configuration saved. Restart through the normal supervisor when client trust is ready.'
return $result
