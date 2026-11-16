#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [string]$CertificatePath,

    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9A-Fa-f]{64}$')]
    [string]$ExpectedSha256
)

$ErrorActionPreference = 'Stop'
$file = (Resolve-Path -LiteralPath $CertificatePath -ErrorAction Stop).ProviderPath
$bytes = [IO.File]::ReadAllBytes($file)
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $hash = [BitConverter]::ToString($sha256.ComputeHash($bytes)).Replace('-', '')
    if ($hash -ne $ExpectedSha256) { throw 'Certificate SHA-256 does not match the trusted fingerprint.' }

    $type = [Security.Cryptography.X509Certificates.X509Certificate2]::GetCertContentType($bytes)
    if ($type -ne [Security.Cryptography.X509Certificates.X509ContentType]::Cert) {
        throw 'Supply one public certificate in DER (.cer) format.'
    }
    $certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new($bytes)
    try {
        $certificateHash = [BitConverter]::ToString($sha256.ComputeHash($certificate.RawData)).Replace('-', '')
        if ($certificateHash -ne $hash -or $certificate.HasPrivateKey) { throw 'Supply one public certificate in DER (.cer) format.' }
        $now = Get-Date
        if ($now -lt $certificate.NotBefore -or $now -gt $certificate.NotAfter) { throw 'The certificate is outside its validity period.' }
        if ([Convert]::ToBase64String($certificate.SubjectName.RawData) -cne [Convert]::ToBase64String($certificate.IssuerName.RawData)) {
            throw 'Use the approved root CA certificate or a self-signed server certificate.'
        }

        $target = "Cert:\CurrentUser\Root\$($certificate.Thumbprint)"
        $status = 'AlreadyTrusted'
        if (!(Test-Path -LiteralPath $target)) {
            if (!$PSCmdlet.ShouldProcess($target, "Trust certificate $($certificate.Subject) with SHA-256 $hash")) { return }
            $store = [Security.Cryptography.X509Certificates.X509Store]::new('Root', 'CurrentUser')
            try {
                $store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
                $store.Add($certificate)
            } finally {
                $store.Close()
            }
            if (!(Test-Path -LiteralPath $target)) { throw 'Windows did not record the trusted certificate.' }
            $status = 'Imported'
        }
        [pscustomobject]@{ Status = $status; Subject = $certificate.Subject; SHA256 = $hash; Store = $target }
    } finally {
        $certificate.Dispose()
    }
} finally {
    $sha256.Dispose()
}
