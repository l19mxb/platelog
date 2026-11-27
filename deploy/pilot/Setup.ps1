#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PythonPath,
    [string]$SiteName,
    [string]$DataDirectory = (Join-Path $env:ProgramData 'PlateLog\pilot-data'),
    [string]$ConfigurationDirectory = (Join-Path $env:ProgramData 'PlateLog\config'),
    [string]$RuntimeAccount = 'NT AUTHORITY\LOCAL SERVICE',
    [ValidateSet('ITManaged','PlateLogGenerated')][string]$CertificateMode,
    [ValidateRange(1,730)][int]$CertificateDays = 365,
    [string[]]$AdditionalCertificateName = @(),
    [string]$CertificatePath,
    [string]$PrivateKeyPath,
    [string]$TrustBundlePath,
    [ValidateRange(1,65535)][int]$StaffPort = 443,
    [ValidateRange(1,65535)][int]$CameraPort = 8766,
    [SecureString]$AdminPassword,
    [switch]$NonInteractive,
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
$package = [IO.Path]::GetFullPath($PSScriptRoot)
$seen = @{}
foreach ($line in Get-Content -LiteralPath (Join-Path $package 'SHA256SUMS.txt')) {
    if ($line -notmatch '^([a-fA-F0-9]{64})  ((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+)$') { throw 'Invalid package manifest.' }
    $digest = $Matches[1]; $relative = $Matches[2]
    if ($seen.ContainsKey($relative) -or ($relative -split '/') -contains '..') { throw 'Duplicate or unsafe manifest path.' }
    $target = Join-Path $package $relative
    if ((Get-Item -LiteralPath $target -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Package members cannot be links.' }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $digest) { throw "Package checksum mismatch: $relative" }
    $seen[$relative] = $true
}
foreach ($required in 'Setup.ps1','Start-PlateLog.ps1','Set-PlateLogCertificate.ps1','setup/CertificateSetup.ps1','setup/check_certificate.py','setup/bootstrap_admin.py','app/platelog/__main__.py','requirements.offline.txt') {
    if (!$seen.ContainsKey($required)) { throw "Missing manifest member: $required" }
}
Write-Host "Verified $($seen.Count) package files."
if ($VerifyOnly) { return }

. (Join-Path $package 'setup\CertificateSetup.ps1')
$null = Get-PlateLogFullPath $package
$python = (Resolve-Path -LiteralPath (Get-PlateLogFullPath $PythonPath)).ProviderPath
$data = Get-PlateLogFullPath $DataDirectory
$configuration = Get-PlateLogFullPath $ConfigurationDirectory
if ($data -eq $configuration -or $data.StartsWith($configuration+'\',[StringComparison]::OrdinalIgnoreCase) -or $configuration.StartsWith($data+'\',[StringComparison]::OrdinalIgnoreCase)) {
    throw 'Keep the writable data directory separate from the read-only configuration directory.'
}
if ($data -eq $package -or $data.StartsWith($package+'\',[StringComparison]::OrdinalIgnoreCase) -or $configuration -eq $package -or $configuration.StartsWith($package+'\',[StringComparison]::OrdinalIgnoreCase)) {
    throw 'Keep data and configuration outside the application package directory.'
}
if (Test-Path -LiteralPath $data) { throw 'The data directory already exists. This installer is for a new installation; use the certificate renewal command for an existing one.' }
if (Test-Path -LiteralPath (Join-Path $configuration 'installation.json')) { throw 'This installation is already configured. Use Start-PlateLog.ps1 or the certificate renewal command.' }
if ($StaffPort -eq $CameraPort) { throw 'Use different staff and camera ports.' }
if (!$SiteName) {
    if ($NonInteractive) { throw '-SiteName is required for non-interactive setup.' }
    $SiteName = Read-Host 'PlateLog server DNS name or IPv4 address (without https://)'
}
$SiteName = Get-PlateLogServerName $SiteName
$runtimeSid = Get-PlateLogRuntimeSid $RuntimeAccount
if ($NonInteractive -and !$AdminPassword) { throw '-AdminPassword must be supplied as a SecureString for non-interactive setup.' }
if ($AdminPassword) {
    $testPassword = [PSCredential]::new('setup',$AdminPassword).GetNetworkCredential().Password
    $validPassword = $testPassword.Length -ge 14 -and [Text.Encoding]::UTF8.GetByteCount($testPassword) -le 1024
    $testPassword = $null
    if (!$validPassword) { throw 'Use an Admin passphrase of 14+ characters, no more than 1,024 UTF-8 bytes.' }
}
& $python -I -B (Join-Path $package 'setup\bootstrap_admin.py') --check-runtime | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'The selected Python runtime did not pass the offline runtime check.' }

$certArguments = @{ConfigurationDirectory=$configuration;PythonPath=$python;Validator=(Join-Path $package 'setup\check_certificate.py');
    ServerName=$SiteName;RuntimeSid=$runtimeSid;Mode=$CertificateMode;AdditionalName=$AdditionalCertificateName;
    CertificatePath=$CertificatePath;PrivateKeyPath=$PrivateKeyPath;TrustBundlePath=$TrustBundlePath;DisallowedDirectory=@($package,$data);NonInteractive=$NonInteractive}
if ($PSBoundParameters.ContainsKey('CertificateDays')) { $certArguments.Days = $CertificateDays }
# A completed installation is rejected above. An interrupted fresh setup may
# replace its own pending certificate configuration without resetting any data.
if (Test-Path -LiteralPath (Join-Path $configuration 'certificate.json')) { $certArguments.Replace = $true }
$certificate = Set-PlateLogCertificateConfiguration @certArguments

if (!$AdminPassword) {
    if ($NonInteractive) { throw '-AdminPassword must be supplied as a SecureString for non-interactive setup.' }
    while ($true) {
        $AdminPassword = Read-Host 'Set the PlateLog Admin passphrase (14+ characters)' -AsSecureString
        $confirmation = Read-Host 'Repeat the Admin passphrase' -AsSecureString
        $a = [PSCredential]::new('setup',$AdminPassword).GetNetworkCredential().Password
        $b = [PSCredential]::new('setup',$confirmation).GetNetworkCredential().Password
        $valid = $a.Length -ge 14 -and [Text.Encoding]::UTF8.GetByteCount($a) -le 1024 -and $a -ceq $b
        $a = $null; $b = $null; $confirmation.Dispose()
        if ($valid) { break }
        $AdminPassword.Dispose(); $AdminPassword = $null
        Write-Host 'Use matching passphrases of 14+ characters, no more than 1,024 UTF-8 bytes.'
    }
}

$venv = Get-PlateLogFullPath (Join-Path $package '.venv')
if (!(Test-Path -LiteralPath (Join-Path $venv 'Scripts\python.exe'))) {
    & $python -I -B -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment setup failed.' }
}
$appPython = Join-Path $venv 'Scripts\python.exe'
$null = Get-PlateLogFullPath $appPython
& $appPython -I -B (Join-Path $package 'setup\bootstrap_admin.py') --check-runtime | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'The existing application virtual environment does not use an approved runtime. Use a new application package directory.' }
& $appPython -I -m pip --isolated --disable-pip-version-check --no-input install `
    --no-index --no-cache-dir --only-binary=:all: --require-hashes --no-compile `
    --find-links (Join-Path $package 'wheels') -r (Join-Path $package 'requirements.offline.txt') | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Offline dependency installation failed.' }
& $appPython -I -m pip --isolated --disable-pip-version-check check | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed.' }

$null = New-PlateLogOwnedDirectory $data $runtimeSid -Modify
$bootstrap = Join-Path $package 'setup\bootstrap_admin.py'
$app = Join-Path $package 'app'
# ProcessStartInfo has no ArgumentList in Windows PowerShell 5.1. Paths cannot
# contain quotes; quote and double trailing backslashes for native argv parsing.
function Quote-NativeArgument([string]$Value) {
    if ($Value.Contains('"')) { throw 'Setup paths cannot contain quote characters.' }
    return '"' + [regex]::Replace($Value,'(\\+)$','$1$1') + '"'
}
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $appPython
$start.Arguments = (@('-I','-B',$bootstrap,'--app-dir',$app,'--data-dir',$data) | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
$start.UseShellExecute = $false; $start.CreateNoWindow = $true
$start.RedirectStandardInput = $true; $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
$process = [Diagnostics.Process]::new(); $process.StartInfo = $start
$pointer = [IntPtr]::Zero
try {
    if (!$process.Start()) { throw 'Could not start Admin configuration.' }
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($AdminPassword)
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringUni($pointer)
    $payload = @{password=$plain} | ConvertTo-Json -Compress
    # Use explicit UTF-8 bytes: Windows PowerShell 5.1's redirected input may
    # otherwise use the Windows ANSI code page for non-ASCII passphrases.
    $inputBytes = [Text.UTF8Encoding]::new($false).GetBytes($payload)
    $process.StandardInput.BaseStream.Write($inputBytes,0,$inputBytes.Length)
    $process.StandardInput.BaseStream.Flush(); $process.StandardInput.Close()
    [Array]::Clear($inputBytes,0,$inputBytes.Length)
    $plain = $null; $payload = $null; $inputBytes = $null
    $output = $process.StandardOutput.ReadToEndAsync(); $errors = $process.StandardError.ReadToEndAsync()
    if (!$process.WaitForExit(60000)) { $process.Kill(); throw 'Admin configuration timed out.' }
    if ($process.ExitCode -ne 0) { throw $errors.Result }
    $null = $output.Result | ConvertFrom-Json
} finally {
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeGlobalAllocUnicode($pointer) }
    if ($inputBytes) { [Array]::Clear($inputBytes,0,$inputBytes.Length) }
    $plain = $null; $payload = $null; $inputBytes = $null; $process.Dispose()
}
Set-PlateLogRestrictedAcl $package $runtimeSid
$settings = [ordered]@{SchemaVersion=1;PackageDirectory=$package;DataDirectory=$data;ConfigurationDirectory=$configuration;
    SiteName=$SiteName;ListenAddress='0.0.0.0';StaffPort=$StaffPort;CameraPort=$CameraPort;
    RuntimeAccount=$RuntimeAccount;RuntimeSid=$runtimeSid.Value;CreatedUtc=[DateTimeOffset]::UtcNow.ToString('o')}
$settingsPath = Join-Path $configuration 'installation.json'
Write-PlateLogJson $settingsPath $settings
Set-PlateLogRestrictedAcl $settingsPath $runtimeSid
Write-Host ''
Write-Host 'PlateLog setup is ready. No firewall rule, Windows trust store or startup service was changed.'
Write-Host "Start with: .\Start-PlateLog.ps1 -SettingsPath '$settingsPath'"
Write-Host "Open https://$($SiteName):$StaffPort/admin and set staff access before sharing the link."
Write-Host 'IT should use its approved supervisor and the selected runtime identity for reboot startup.'
[pscustomobject]@{Result='Ready';SettingsPath=$settingsPath;CertificateMode=$certificate.Mode;CertificateExpiresUtc=$certificate.ExpiresUtc;StaffPort=$StaffPort;CameraPort=$CameraPort;AutomaticRenewal=$false}
