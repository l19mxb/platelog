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
    $plate = 'L19MXB'
    $cameraAddress = '192.0.2.1'
    $cameraImage = '/local/fflprapp/tools.cgi?action=getImage&name=testplate.jpg'
    $event = [ordered]@{
        packetCounter = [string]$milliseconds
        capture_timestamp = [string]$milliseconds
        frame_timestamp = [string]$milliseconds
        capture_ts = [string]($milliseconds * 1000000)
        datetime = $now.ToString('yyyyMMdd HHmmssfff', [Globalization.CultureInfo]::InvariantCulture)
        plateText = $plate
        plateUnicode = $plate
        plateUTF8 = $plate
        plateASCII = $plate
        plateCountry = 'AUS'
        'plateISO3166-2' = 'AU-NSW'
        plateType = '(null)'
        plateRegion = 'NSW'
        plateRegionCode = 'NSW'
        plateList = ''
        plateListMode = ''
        plateListDescription = ''
        plateConfidence = '0.950000'
        carState = 'new'
        roiID = '1'
        geotag = [ordered]@{ lat = 0.0; lon = 0.0 }
        imageType = 'plate'
        plateImageType = 'jpeg'
        plateImageSize = '0'
        carMoveDirection = 'in'
        timeProcessing = '0'
        plateCoordinates = @(0, 0, 0, 0)
        plateCoordinatesRelative = @(0, 0, 0, 0)
        carID = [string](Get-Random -Minimum 100000 -Maximum 999999)
        GEOtarget = 'Camera'
        imagesURI = @($cameraImage, $cameraImage)
        ImageArray = @([ordered]@{
            ImageType = ''
            ImageFormat = 'jpg'
            BinaryImage = [Convert]::ToBase64String($photo)
        })
        imageFile = 'localdata/images/testplate.jpg'
        imageFile2 = 'localdata/images/testplate.jpg'
        profileID = '1'
        vehicle_info = [ordered]@{ view = 'front'; type = 'CAR'; make = 'Subaru'; model = 'WRX Wagon'; color = 'BLACK' }
        camera_info = [ordered]@{
            SerialNumber = '020000000001'
            ProdShortName = 'AXIS Q1700-LE'
            IPAddress = $cameraAddress
            MACAddress = '020000000001'
        }
        sensorProviderID = $cameraAddress
    }
    # The Q1700-LE uses JSON Unicode escapes in these two fields and plain text in the UTF-8 and ASCII fields.
    $escapedPlate = ($plate.ToCharArray() | ForEach-Object { '\u{0:X4}' -f [int]$_ }) -join ''
    $json = $event | ConvertTo-Json -Depth 8 -Compress
    foreach ($field in 'plateText', 'plateUnicode') {
        $json = $json.Replace(('"{0}":"{1}"' -f $field, $plate), ('"{0}":"{1}"' -f $field, $escapedPlate))
    }
    $body = [Text.Encoding]::UTF8.GetBytes($json)
}

$headers = @{}
if ($Credential) {
    if ($Credential.UserName.Contains(':')) { throw 'HTTP Basic usernames cannot contain a colon.' }
    $pair = '{0}:{1}' -f $Credential.UserName, $Credential.GetNetworkCredential().Password
    $headers.Authorization = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
}

Invoke-RestMethod -Uri $Uri.AbsoluteUri -Method Post -ContentType $ContentType -Headers $headers `
    -Body $body -TimeoutSec 30 -MaximumRedirection 0 -UseBasicParsing
