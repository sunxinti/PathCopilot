param(
    [switch]$Download,
    [string]$Python = "python",
    [string]$GdcClient = "gdc-client"
)

$root = Split-Path -Parent $PSScriptRoot
$arguments = @(
    (Join-Path $PSScriptRoot "gdc_download.py"),
    "--project", "TCGA-COAD",
    "--id-file", (Join-Path $root "ids\tcga_coad_svs_files.txt"),
    "--work-dir", (Join-Path $root "work\tcga_coad"),
    "--gdc-client", $GdcClient
)
if ($Download) { $arguments += "--download" }
& $Python @arguments
exit $LASTEXITCODE
