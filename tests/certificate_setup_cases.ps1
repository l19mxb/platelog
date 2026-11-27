#Requires -Version 5.1
param([string]$Source,[string]$Python,[string]$Root)
$ErrorActionPreference = 'Stop'
. (Join-Path $Source 'CertificateSetup.ps1')
$sid = Get-PlateLogRuntimeSid 'S-1-5-19'
$validator = Join-Path $Source 'check_certificate.py'
$root = Get-PlateLogFullPath $Root
$base = Join-Path $root 'generated'
$generated = Set-PlateLogCertificateConfiguration -ConfigurationDirectory $base -PythonPath $Python -Validator $validator `
    -ServerName 'platelog.example.internal' -RuntimeSid $sid -Mode PlateLogGenerated -Days 180 `
    -AdditionalName '127.0.0.1','alias.example.internal' -NonInteractive
$certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new([IO.File]::ReadAllBytes($generated.PublicCertificatePath))
$thumbprint = $certificate.Thumbprint; $keyBits = [Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPublicKey($certificate).KeySize
$certificate.Dispose()
$old = Get-Content -LiteralPath (Join-Path $base 'certificate.json') -Raw
$originalKeyHash = (Get-FileHash -LiteralPath $generated.PrivateKeyPath -Algorithm SHA256).Hash
$default = Set-PlateLogCertificateConfiguration -ConfigurationDirectory (Join-Path $root 'default') -PythonPath $Python -Validator $validator `
    -ServerName 'other.example.internal' -RuntimeSid $sid -Mode PlateLogGenerated -NonInteractive
$managed = Set-PlateLogCertificateConfiguration -ConfigurationDirectory (Join-Path $root 'managed') -PythonPath $Python -Validator $validator `
    -ServerName 'platelog.example.internal' -RuntimeSid $sid -Mode ITManaged -Days 1 `
    -CertificatePath $generated.CertificatePath -PrivateKeyPath $generated.PrivateKeyPath -TrustBundlePath $generated.TrustBundlePath -NonInteractive
try {
    $null = Set-PlateLogCertificateConfiguration -ConfigurationDirectory $base -PythonPath $Python -Validator $validator `
        -ServerName 'platelog.example.internal' -RuntimeSid $sid -Mode ITManaged -Replace `
        -CertificatePath $generated.CertificatePath -PrivateKeyPath $default.PrivateKeyPath -TrustBundlePath $generated.TrustBundlePath -NonInteractive
    throw 'The mismatched key was accepted.'
} catch {
    if ($_.Exception.Message -notlike 'TLS validation failed.*') { throw }
}
if ((Get-Content -LiteralPath (Join-Path $base 'certificate.json') -Raw) -cne $old) { throw 'Failed validation changed the active certificate.' }
$renewed = Set-PlateLogCertificateConfiguration -ConfigurationDirectory $base -PythonPath $Python -Validator $validator `
    -ServerName 'platelog.example.internal' -RuntimeSid $sid -Mode PlateLogGenerated -Days 90 -Replace -NonInteractive
if (!(Test-Path -LiteralPath $generated.PrivateKeyPath)) { throw 'Renewal removed the old key.' }
if ((Get-FileHash -LiteralPath $generated.PrivateKeyPath -Algorithm SHA256).Hash -ne $originalKeyHash) { throw 'IT-managed selection changed supplied material.' }
$trusted = @()
foreach ($location in 'CurrentUser','LocalMachine') {
    foreach ($name in 'Root','My') {
        $store = [Security.Cryptography.X509Certificates.X509Store]::new($name,$location)
        try {
            $store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly)
            $trusted += @($store.Certificates | Where-Object Thumbprint -EQ $thumbprint)
        } finally { $store.Close() }
    }
}
if ($trusted.Count) { throw 'Setup changed a Windows certificate store.' }
$rejected = @{}
foreach ($kind in 'expired','future','missing-san','wrong-purpose') {
    $fixture = New-PlateLogOwnedDirectory (Join-Path $root $kind) $sid
    $key = [Security.Cryptography.RSACng]::new(2048)
    $cert = $null
    try {
        $req = [Security.Cryptography.X509Certificates.CertificateRequest]::new('CN=platelog.example.internal',$key,
            [Security.Cryptography.HashAlgorithmName]::SHA256,[Security.Cryptography.RSASignaturePadding]::Pkcs1)
        [void]$req.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false,$false,0,$true))
        if ($kind -ne 'missing-san') {
            $san = [Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
            $san.AddDnsName('platelog.example.internal'); [void]$req.CertificateExtensions.Add($san.Build())
        }
        $oids = [Security.Cryptography.OidCollection]::new()
        [void]$oids.Add([Security.Cryptography.Oid]::new($(if($kind -eq 'wrong-purpose'){'1.3.6.1.5.5.7.3.2'}else{'1.3.6.1.5.5.7.3.1'})))
        [void]$req.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($oids,$true))
        $now = [DateTimeOffset]::UtcNow
        $before = $now.AddMinutes(-5); $after = $now.AddDays(10)
        if ($kind -eq 'expired') { $before = $now.AddDays(-2); $after = $now.AddDays(-1) }
        if ($kind -eq 'future') { $before = $now.AddDays(1) }
        $cert = $req.CreateSelfSigned($before,$after)
        $cp = Join-Path $fixture 'server.pem'; $kp = Join-Path $fixture 'server-key.pem'
        [IO.File]::WriteAllText($cp,(ConvertTo-PlateLogPem 'CERTIFICATE' $cert.RawData),[Text.Encoding]::ASCII)
        [IO.File]::WriteAllText($kp,(Export-PlateLogRsaPem $key),[Text.Encoding]::ASCII)
        try {
            $null = Test-PlateLogCertificate $Python $validator $cp $kp 'platelog.example.internal' $cp
            throw "Invalid fixture accepted: $kind"
        } catch {
            if ($_.Exception.Message -notlike 'TLS validation failed.*') { throw }
            $rejected[$kind] = $true
        }
    } finally { if($cert){$cert.Dispose()}; $key.Dispose() }
}
$script:answers = [Collections.Generic.Queue[string]]::new()
function Read-Host { param([string]$Prompt) return $script:answers.Dequeue() }
$script:answers.Enqueue(''); $preferred = Read-PlateLogCertificateMode
$script:answers.Enqueue('2'); $fallback = Read-PlateLogCertificateMode
foreach ($answer in '0','731','bad','90') { $script:answers.Enqueue($answer) }
$chosen = Read-PlateLogCertificateDays
$invalid = @()
foreach ($name in 'https://wrong.example/','x;whoami','*.example.internal','x, CN=other','name:443') {
    try { $null = Get-PlateLogServerName $name; throw 'Unsafe server name accepted.' } catch { if ($_.Exception.Message -eq 'Unsafe server name accepted.') { throw } }
    $invalid += $name
}
$rules = (Get-Acl -LiteralPath $generated.PrivateKeyPath).GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])
$runtimeRules = @($rules | Where-Object { $_.IdentityReference.Value -eq 'S-1-5-19' })
if (!$runtimeRules.Count -or @($runtimeRules | Where-Object { $_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::Write }).Count) { throw 'Runtime key permissions are not read-only.' }
$result = @{generated=$generated;default=$default;managed=$managed;renewed=$renewed;failed_validation_preserved=$true;
    old_key_preserved=$true;windows_store_unchanged=$true;default_menu=$preferred;fallback_menu=$fallback;chosen_days=$chosen;
    rsa_bits=$keyBits;invalid_names_rejected=$invalid.Count;key_runtime_read_only=$true;invalid_certificates=$rejected;ps_version=$PSVersionTable.PSVersion.ToString()}
Write-PlateLogJson (Join-Path $root 'cases.json') $result
