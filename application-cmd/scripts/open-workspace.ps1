$deadline = (Get-Date).AddSeconds(60)
$url = 'http://127.0.0.1:5000/workspace'
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Start-Process $url
            exit 0
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
Write-Warning "PathCopilot did not become ready within 60 seconds: $url"
exit 1

