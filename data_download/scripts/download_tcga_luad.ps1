param(
    [switch]$Download,
    [string]$Python = "python",
    [string]$GdcClient = "gdc-client"
)

$root = Split-Path -Parent $PSScriptRoot
$arguments = @(
    (Join-Path $PSScriptRoot "gdc_download.py"),
    "--project", "TCGA-LUAD",
    "--id-file", (Join-Path $root "ids\tcga_luad_svs_files.txt"),
    "--work-dir", (Join-Path $root "work\tcga_luad"),
    "--gdc-client", $GdcClient
)
if ($Download) { $arguments += "--download" }
& $Python @arguments
exit $LASTEXITCODE
