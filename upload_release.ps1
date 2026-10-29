$filePath = "C:\Users\mohda\.gemini\antigravity-ide\scratch\Mk-tool\dist\MkTool_v1.0.0_Setup.exe"
$token = "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"
$url = "https://uploads.github.com/repos/mohdkhan-lab/Mk-tool/releases/428042741/assets?name=MkTool_v1.0.0_Setup.exe"

if (-not (Test-Path $filePath)) {
    Write-Error "File not found: $filePath"
    exit 1
}

$handler = [System.Net.Http.HttpClientHandler]::new()
$client = [System.Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromMinutes(25)
$client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $token)
$client.DefaultRequestHeaders.UserAgent.ParseAdd("MkTool-Publisher")
$client.DefaultRequestHeaders.Accept.ParseAdd("application/vnd.github+json")

$fileStream = [System.IO.File]::OpenRead($filePath)
$content = [System.Net.Http.StreamContent]::new($fileStream)
$content.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::new("application/octet-stream")
$content.Headers.ContentLength = $fileStream.Length

Write-Output "Uploading $([math]::Round($fileStream.Length / 1MB, 2)) MB to GitHub Release asset..."
$response = $client.PostAsync($url, $content).Result
$body = $response.Content.ReadAsStringAsync().Result
Write-Output "HTTP Status: $($response.StatusCode)"
$fileStream.Close()

if ($response.IsSuccessStatusCode) {
    Write-Output "UPLOAD SUCCESSFUL!"
} else {
    Write-Output "Upload failed: $body"
    exit 1
}

