# DEV_ONLY: ephemeral in-memory key, public output only. Windows .NET crypto provider.
$ErrorActionPreference = 'Stop'
$ecdsa = [System.Security.Cryptography.ECDsa]::Create()
if ($null -eq $ecdsa) { throw 'ECDsa provider not available' }
try {
    $ecdsa.GenerateKey([System.Security.Cryptography.ECCurve]::CreateFromFriendlyName('nistP256'))
    $data = [System.Text.Encoding]::UTF8.GetBytes('SCN/B04/dev-only/cng-interoperability-v1')
    $format = [System.Security.Cryptography.DSASignatureFormat]::IeeeP1363FixedFieldConcatenation
    $sig = $ecdsa.SignData($data, [System.Security.Cryptography.HashAlgorithmName]::SHA256, $format)
    $pub = $ecdsa.ExportSubjectPublicKeyInfo()
    [pscustomobject]@{
        data = [Convert]::ToBase64String($data)
        spki = [Convert]::ToBase64String($pub)
        signature = [Convert]::ToBase64String($sig)
        length = $sig.Length
        provider_type = $ecdsa.GetType().Name
    } | ConvertTo-Json -Compress
} finally { $ecdsa.Dispose() }

