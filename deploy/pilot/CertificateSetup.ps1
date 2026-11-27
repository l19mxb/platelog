#Requires -Version 5.1
# Shared functions only. Dot-sourcing this file makes no system or trust-store changes.
Set-StrictMode -Version Latest

function Get-PlateLogFullPath([string]$Path) {
    if (![IO.Path]::IsPathRooted($Path)) { throw 'Use an absolute filesystem path.' }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($full.Length -le ([IO.Path]::GetPathRoot($full)).TrimEnd('\').Length) { throw 'Choose a dedicated folder, not a drive or share root.' }
    $item = $full
    while ($item) {
        if (Test-Path -LiteralPath $item) {
            if ((Get-Item -LiteralPath $item -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Setup locations cannot use symbolic links or junctions.'
            }
        }
        $parent = [IO.Path]::GetDirectoryName($item)
        if ($parent -eq $item) { break }
        $item = $parent
    }
    return $full
}

function Get-PlateLogServerName([string]$Name) {
    $value = $Name.Trim().TrimEnd('.').ToLowerInvariant()
    $ip = $null
    if ([Net.IPAddress]::TryParse($value, [ref]$ip)) {
        if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'Use a DNS name or IPv4 server address.' }
        return $ip.ToString()
    }
    if ($value.Length -gt 253 -or $value -notmatch '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$') {
        throw 'Enter the server DNS name or IPv4 address without a URL, port, wildcard or path.'
    }
    return $value
}

function Get-PlateLogRuntimeSid([string]$Account) {
    if ($Account -match '^S-1-') { $sid = [Security.Principal.SecurityIdentifier]::new($Account) }
    else { $sid = ([Security.Principal.NTAccount]::new($Account)).Translate([Security.Principal.SecurityIdentifier]) }
    if ($sid.Value -in 'S-1-1-0','S-1-5-11','S-1-5-18','S-1-5-32-544') { throw 'Choose a dedicated non-administrator runtime account. Local Service is the default.' }
    return $sid
}

function Set-PlateLogRestrictedAcl([string]$Path, [Security.Principal.SecurityIdentifier]$RuntimeSid, [switch]$Modify) {
    $isDirectory = (Get-Item -LiteralPath $Path -Force) -is [IO.DirectoryInfo]
    $acl = if ($isDirectory) { [Security.AccessControl.DirectorySecurity]::new() } else { [Security.AccessControl.FileSecurity]::new() }
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = if ($isDirectory) { [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' } else { [Security.AccessControl.InheritanceFlags]::None }
    $propagate = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($sid in @($ownerSid, [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'), [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, [Security.AccessControl.FileSystemRights]::FullControl, $inherit, $propagate, $allow))
    }
    $rights = if ($Modify) { [Security.AccessControl.FileSystemRights]::Modify } else { [Security.AccessControl.FileSystemRights]::ReadAndExecute }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($RuntimeSid, $rights, $inherit, $propagate, $allow))
    Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
}

function New-PlateLogOwnedDirectory([string]$Path, [Security.Principal.SecurityIdentifier]$RuntimeSid, [switch]$Modify) {
    $full = Get-PlateLogFullPath $Path
    if (Test-Path -LiteralPath $full) { throw "The dedicated setup folder already exists: $full" }
    $null = [IO.Directory]::CreateDirectory($full)
    Set-PlateLogRestrictedAcl $full $RuntimeSid -Modify:$Modify
    return $full
}

function Write-PlateLogJson([string]$Path, $Value) {
    $text = $Value | ConvertTo-Json -Depth 8
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($text + "`n")
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes,0,$bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}

function ConvertTo-PlateLogPem([string]$Label, [byte[]]$Bytes) {
    $body = [Convert]::ToBase64String($Bytes, [Base64FormattingOptions]::InsertLineBreaks)
    return "-----BEGIN $Label-----`r`n$body`r`n-----END $Label-----`r`n"
}

function ConvertTo-PlateLogDer([byte]$Tag, [byte[]]$Body) {
    $header = [Collections.Generic.List[byte]]::new()
    $header.Add($Tag)
    if ($Body.Length -lt 128) { $header.Add([byte]$Body.Length) }
    else {
        $length = [Collections.Generic.List[byte]]::new()
        $number = $Body.Length
        while ($number -gt 0) { $length.Insert(0,[byte]($number -band 255)); $number = $number -shr 8 }
        $header.Add([byte](128 + $length.Count)); $header.AddRange($length)
    }
    $header.AddRange($Body)
    return ,$header.ToArray()
}

function Export-PlateLogRsaPem([Security.Cryptography.RSA]$Key) {
    # PKCS#1 DER avoids PowerShell 7-only ExportPkcs8PrivateKeyPem APIs.
    $p = $Key.ExportParameters($true)
    $body = [Collections.Generic.List[byte]]::new()
    foreach ($number in @([byte[]]@(0), $p.Modulus, $p.Exponent, $p.D, $p.P, $p.Q, $p.DP, $p.DQ, $p.InverseQ)) {
        [byte[]]$integer = $number
        $offset = 0
        while ($offset -lt ($integer.Length-1) -and $integer[$offset] -eq 0) { $offset++ }
        $integer = $integer[$offset..($integer.Length-1)]
        if ($integer[0] -band 128) { $integer = [byte[]]@(0) + $integer }
        $encoded = ConvertTo-PlateLogDer 2 $integer
        $body.AddRange($encoded)
    }
    return ConvertTo-PlateLogPem 'RSA PRIVATE KEY' (ConvertTo-PlateLogDer 48 $body.ToArray())
}

function New-PlateLogSelfSignedCertificate([string]$Directory, [string]$ServerName, [string[]]$AdditionalName, [int]$Days) {
    if ($Days -lt 1 -or $Days -gt 730) { throw 'Choose a certificate lifetime from 1 to 730 days.' }
    $names = @(@($ServerName) + $AdditionalName | ForEach-Object { Get-PlateLogServerName $_ } | Select-Object -Unique)
    $key = [Security.Cryptography.RSACng]::new(3072)
    $certificate = $null
    try {
        $request = [Security.Cryptography.X509Certificates.CertificateRequest]::new(
            "CN=$ServerName",$key,[Security.Cryptography.HashAlgorithmName]::SHA256,[Security.Cryptography.RSASignaturePadding]::Pkcs1)
        [void]$request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false,$false,0,$true))
        $usage = [Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature -bor [Security.Cryptography.X509Certificates.X509KeyUsageFlags]::KeyEncipherment
        [void]$request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new($usage,$true))
        $purposes = [Security.Cryptography.OidCollection]::new()
        $null = $purposes.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.1'))
        [void]$request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($purposes,$true))
        $san = [Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
        foreach ($name in $names) {
            $address = $null
            if ([Net.IPAddress]::TryParse($name,[ref]$address)) { $san.AddIpAddress($address) } else { $san.AddDnsName($name) }
        }
        [void]$request.CertificateExtensions.Add($san.Build())
        $now = [DateTimeOffset]::UtcNow
        $certificate = $request.CreateSelfSigned($now.AddMinutes(-5),$now.AddDays($Days))
        $certPath = Join-Path $Directory 'server.pem'
        $keyPath = Join-Path $Directory 'server-key.pem'
        $publicPath = Join-Path $Directory 'platelog-server.cer'
        [IO.File]::WriteAllText($certPath,(ConvertTo-PlateLogPem 'CERTIFICATE' $certificate.RawData),[Text.Encoding]::ASCII)
        [IO.File]::WriteAllText($keyPath,(Export-PlateLogRsaPem $key),[Text.Encoding]::ASCII)
        [IO.File]::WriteAllBytes($publicPath,$certificate.RawData)
        $hash = (Get-FileHash -LiteralPath $publicPath -Algorithm SHA256).Hash.ToLowerInvariant()
        [IO.File]::WriteAllText(($publicPath+'.sha256'),($hash+'  platelog-server.cer'+"`n"),[Text.Encoding]::ASCII)
        return [pscustomobject]@{CertificatePath=$certPath;PrivateKeyPath=$keyPath;TrustBundlePath=$certPath;PublicCertificatePath=$publicPath;PublicCertificateSHA256=$hash}
    } finally {
        if ($certificate) { $certificate.Dispose() }
        $key.Dispose()
    }
}

function Read-PlateLogCertificateMode {
    Write-Host ''
    Write-Host 'HTTPS certificate'
    Write-Host '  1. IT-managed certificate (preferred) - IT supplies and maintains it.'
    Write-Host '  2. PlateLog-generated certificate (fallback) - choose its lifetime.'
    while ($true) {
        $choice = (Read-Host 'Certificate option [1]').Trim()
        if ($choice -in '','1') { return 'ITManaged' }
        if ($choice -eq '2') { return 'PlateLogGenerated' }
        Write-Host 'Choose 1 or 2.'
    }
}

function Read-PlateLogCertificateDays([int]$Default = 365) {
    Write-Host 'Lifetime in days: 90, 180, 365, 730, or another value from 1 to 730.'
    while ($true) {
        $answer = (Read-Host "Certificate lifetime [$Default]").Trim()
        if (!$answer) { return $Default }
        $days = 0
        if ([int]::TryParse($answer,[ref]$days) -and $days -ge 1 -and $days -le 730) { return $days }
        Write-Host 'Choose a whole number from 1 to 730 days.'
    }
}

function Test-PlateLogCertificate([string]$PythonPath, [string]$Validator, [string]$CertificatePath,
                                  [string]$PrivateKeyPath, [string]$ServerName, [string]$TrustBundlePath) {
    $nativeArguments = @('-I','-B',$Validator,'--certificate',$CertificatePath,'--private-key',$PrivateKeyPath,'--server-name',$ServerName)
    if ($TrustBundlePath) { $nativeArguments += @('--trust-ca',$TrustBundlePath) }
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $PythonPath @nativeArguments 2>&1
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) { throw ('TLS validation failed. '+($output | Out-String).Trim()) }
    return ($output | Out-String | ConvertFrom-Json)
}

function Set-PlateLogCertificateConfiguration {
    [CmdletBinding()]
    param([string]$ConfigurationDirectory,[string]$PythonPath,[string]$Validator,[string]$ServerName,
          [Security.Principal.SecurityIdentifier]$RuntimeSid,[string]$Mode,[int]$Days=365,[int]$DefaultDays=365,
          [string[]]$AdditionalName=@(),[string]$CertificatePath,[string]$PrivateKeyPath,[string]$TrustBundlePath,[string[]]$DisallowedDirectory=@(),
          [switch]$Replace,[switch]$NonInteractive)
    $root = Get-PlateLogFullPath $ConfigurationDirectory
    $name = Get-PlateLogServerName $ServerName
    $currentPath = Join-Path $root 'certificate.json'
    if ((Test-Path -LiteralPath $currentPath) -and !$Replace) { throw 'A certificate configuration already exists. Use the explicit renewal/reconfiguration command.' }
    $marker = Join-Path $root 'certificate-setup.json'
    if (!(Test-Path -LiteralPath $root)) {
        $null = New-PlateLogOwnedDirectory $root $RuntimeSid
        Write-PlateLogJson $marker @{Owner='PlateLog certificate setup';SchemaVersion=1}
    } else {
        $null = Get-PlateLogFullPath $currentPath
        $null = Get-PlateLogFullPath $marker
        if (!(Test-Path -LiteralPath $marker -PathType Leaf)) { throw 'Use a new dedicated configuration folder; setup will not change an existing unrecognised folder.' }
        $owned = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
        if ($owned.Owner -ne 'PlateLog certificate setup' -or $owned.SchemaVersion -ne 1) { throw 'The configuration folder is not a recognised PlateLog setup folder.' }
    }
    if (!$Mode) {
        if ($NonInteractive) { $Mode = 'ITManaged' } else { $Mode = Read-PlateLogCertificateMode }
    }
    if ($Mode -notin 'ITManaged','PlateLogGenerated') { throw 'Choose ITManaged or PlateLogGenerated.' }
    $version = Join-Path $root ('certificate-'+[Guid]::NewGuid().ToString('N'))
    $materials = $null
    if ($Mode -eq 'PlateLogGenerated') {
        if (!$NonInteractive -and !$PSBoundParameters.ContainsKey('Days')) { $Days = Read-PlateLogCertificateDays $DefaultDays }
        if ($Days -lt 1 -or $Days -gt 730) { throw 'Choose a certificate lifetime from 1 to 730 days.' }
        $null = New-PlateLogOwnedDirectory $version $RuntimeSid
        $materials = New-PlateLogSelfSignedCertificate $version $name $AdditionalName $Days
        $CertificatePath = $materials.CertificatePath; $PrivateKeyPath = $materials.PrivateKeyPath; $TrustBundlePath = $materials.TrustBundlePath
    } else {
        if (!$CertificatePath -and !$NonInteractive) { $CertificatePath = Read-Host 'IT certificate chain PEM path (server first, then intermediates)' }
        if (!$PrivateKeyPath -and !$NonInteractive) { $PrivateKeyPath = Read-Host 'Matching private key PEM path' }
        if (!$CertificatePath -or !$PrivateKeyPath) { throw 'IT-managed mode requires a certificate chain and private key in PEM format.' }
        $CertificatePath = (Resolve-Path -LiteralPath (Get-PlateLogFullPath $CertificatePath)).ProviderPath
        $PrivateKeyPath = (Resolve-Path -LiteralPath (Get-PlateLogFullPath $PrivateKeyPath)).ProviderPath
        if ($TrustBundlePath) { $TrustBundlePath = (Resolve-Path -LiteralPath (Get-PlateLogFullPath $TrustBundlePath)).ProviderPath }
        foreach ($material in @($CertificatePath,$PrivateKeyPath,$TrustBundlePath) | Where-Object { $_ }) {
            foreach ($blocked in $DisallowedDirectory) {
                $prefix = (Get-PlateLogFullPath $blocked) + '\'
                if ($material.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Keep IT-managed certificate material outside the application package and writable data directories.'
                }
            }
        }
        # IT keeps ownership of these files, their ACLs and future renewal.
    }
    $tested = Test-PlateLogCertificate $PythonPath $Validator $CertificatePath $PrivateKeyPath $name $TrustBundlePath
    $state = [ordered]@{SchemaVersion=1;Mode=$Mode;ServerName=$name;CertificatePath=$CertificatePath;PrivateKeyPath=$PrivateKeyPath;
        TrustBundlePath=$TrustBundlePath;ValidatedUtc=[DateTimeOffset]::UtcNow.ToString('o');ExpiresUtc=$tested.expires_utc;
        CertificateSHA256=$tested.certificate_sha256;Names=@($tested.names);LifetimeDays=$(if($Mode -eq 'PlateLogGenerated'){$Days}else{$null});
        PublicCertificatePath=$(if($materials){$materials.PublicCertificatePath}else{''});
        PublicCertificateSHA256=$(if($materials){$materials.PublicCertificateSHA256}else{''});
        RenewalOwner=$(if($Mode -eq 'ITManaged'){'IT certificate team'}else{'Site administrator; redistribute renewed public certificate'});
        AutomaticRenewal=$false}
    $staged = Join-Path $root ('certificate-'+[Guid]::NewGuid().ToString('N')+'.json')
    Write-PlateLogJson $staged $state
    Set-PlateLogRestrictedAcl $staged $RuntimeSid
    if (Test-Path -LiteralPath $currentPath) {
        [IO.File]::Replace($staged,$currentPath,(Join-Path $root ('certificate-previous-'+[Guid]::NewGuid().ToString('N')+'.json')))
    } else { [IO.File]::Move($staged,$currentPath) }
    Write-Host ''
    Write-Host "Certificate option: $Mode"
    Write-Host "Server name: $name"
    Write-Host "Expires (UTC): $($tested.expires_utc)"
    Write-Host "Renewal owner: $($state.RenewalOwner). Automatic renewal: No."
    if ($materials) {
        Write-Host "Public certificate for staff PCs and cameras: $($materials.PublicCertificatePath)"
        Write-Host "Public certificate SHA-256: $($materials.PublicCertificateSHA256)"
        Write-Host 'Distribute only the public .cer. Never distribute server-key.pem.'
        Write-Host 'After renewal, trust the new public certificate on clients and restart PlateLog through its normal supervisor.'
    } else { Write-Host 'IT must grant the runtime account read access to these PEM files and restart PlateLog after replacing them.' }
    return [pscustomobject]$state
}
