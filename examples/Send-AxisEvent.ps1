[CmdletBinding(DefaultParameterSetName = 'Demo')]
param(
    [Parameter(Mandatory)]
    [uri]$Uri,

    [Parameter(ParameterSetName = 'Demo')]
    [string]$ImagePath = (Join-Path $PSScriptRoot '../docs/images/demo-l19mxb.jpg'),

    [Parameter(Mandatory, ParameterSetName = 'Replay')]
    [string]$CapturePath,

    [Parameter(ParameterSetName = 'Replay')]
    [string]$ContentType = 'application/json',

    [pscredential]$Credential
)

$ErrorActionPreference = 'Stop'

if (!$Uri.IsAbsoluteUri -or $Uri.Scheme -notin 'http', 'https' -or $Uri.UserInfo) {
    throw 'Use an HTTP or HTTPS receiver URL. Supply credentials separately.'
}

if ($PSCmdlet.ParameterSetName -eq 'Replay') {
    # A capture is the raw HTTP request body. Use the original Content-Type including any boundary.
    $body = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $CapturePath).ProviderPath)
} else {
    $photo = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $ImagePath).ProviderPath)
    $now = [DateTimeOffset]::UtcNow
    $milliseconds = $now.ToUnixTimeMilliseconds()
    $event = [ordered]@{
        packetCounter = [string]$milliseconds
        capture_timestamp = [string]$milliseconds
        frame_timestamp = [string]($milliseconds * 1000)
        capture_ts = [string]($milliseconds * 1000000)
        datetime = $now.ToString('yyyyMMdd HHmmssfff', [Globalization.CultureInfo]::InvariantCulture)
        plateText = 'L19MXB'
        plateUnicode = 'L19MXB'
        plateUTF8 = 'L19MXB'
        plateASCII = 'L19MXB'
        plateCountry = 'AUS'
        'plateISO3166-2' = 'AU-NSW'
        plateConfidence = '0.990000'
        carState = 'lost'
        roiID = '1'
        imageType = 'vehicle'
        plateImageType = 'jpeg'
        plateImageSize = [string]$photo.Length
        carMoveDirection = 'in'
        carID = [string](Get-Random -Minimum 100000 -Maximum 999999)
        profileID = '1'
        ImageArray = @([ordered]@{
            ImageType = 'vehicle'
            ImageFormat = 'jpg'
            BinaryImage = [Convert]::ToBase64String($photo)
        })
        vehicle_info = @{ view = 'front'; type = 'CAR'; color = 'BLACK'; make = 'Subaru'; model = '2023 WRX Sportswagon' }
        camera_info = @{ SerialNumber = '020000000001'; ProdShortName = 'AXIS Q1700-LE'; IPAddress = '192.0.2.1'; MACAddress = '020000000001' }
        sensorProviderID = 'platelog-test'
    }
    $body = [Text.Encoding]::UTF8.GetBytes(($event | ConvertTo-Json -Depth 8 -Compress))
}

$headers = @{}
if ($Credential) {
    if ($Credential.UserName.Contains(':')) { throw 'HTTP Basic usernames cannot contain a colon.' }
    $pair = '{0}:{1}' -f $Credential.UserName, $Credential.GetNetworkCredential().Password
    $headers.Authorization = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
}

Invoke-RestMethod -Uri $Uri.AbsoluteUri -Method Post -ContentType $ContentType -Headers $headers `
    -Body $body -TimeoutSec 30 -MaximumRedirection 0 -UseBasicParsing
