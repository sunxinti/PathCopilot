param(
    [switch]$CheckUrls,
    [switch]$Download,
    [string]$Python = "python",
    [int]$Workers = 4
)

$root = Split-Path -Parent $PSScriptRoot
$arguments = @(
    (Join-Path $PSScriptRoot "cptac_download.py"),
    "--manifest", (Join-Path $root "ids\cptac_ucec_download_manifest.tsv"),
    "--output-dir", (Join-Path $root "work\cptac_ucec"),
    "--workers", $Workers
)
if ($CheckUrls) { $arguments += "--check-urls" }
if ($Download) { $arguments += "--download" }
& $Python @arguments
exit $LASTEXITCODE
