param([string]$RunnerDir = 'D:\GitHubRunner')
$ErrorActionPreference = 'Stop'
$repo = 'Jupiterz128/manhwa-ocr-editor'
$gh = (Get-Command gh.exe -ErrorAction SilentlyContinue).Source
if (!$gh) { $gh = "$env:ProgramFiles\GitHub CLI\gh.exe" }
if ((& $gh api "repos/$repo" --jq '.private') -ne 'true') { throw 'Private repository not verified' }
New-Item -ItemType Directory -Force -Path $RunnerDir | Out-Null
Push-Location -LiteralPath $RunnerDir
try {
    if (Test-Path .runner) { throw 'Runner already configured' }
    if (!(Test-Path .\config.cmd)) {
        if (Get-ChildItem -Force) { throw 'Choose an empty runner directory' }
        $release = (& $gh api repos/actions/runner/releases/latest | Out-String | ConvertFrom-Json)
        $asset = $release.assets | Where-Object name -like 'actions-runner-win-x64-*.zip' | Select-Object -First 1
        if ($asset.digest -notmatch '^sha256:[0-9a-fA-F]{64}$') { throw 'Missing runner SHA256' }
        $zip = Join-Path $env:TEMP ("runner-" + [guid]::NewGuid() + ".zip")
        try {
            curl.exe -fsSL --retry 2 -o $zip $asset.browser_download_url
            if ($LASTEXITCODE -or ("sha256:" + (Get-FileHash -LiteralPath $zip).Hash) -ne $asset.digest) { throw 'Runner download verification failed' }
            Expand-Archive -LiteralPath $zip -DestinationPath .
        } finally { Remove-Item -LiteralPath $zip -ErrorAction SilentlyContinue }
    }
    $env:ACTIONS_RUNNER_INPUT_TOKEN = & $gh api -X POST "repos/$repo/actions/runners/registration-token" --jq '.token'
    if ($LASTEXITCODE -or !$env:ACTIONS_RUNNER_INPUT_TOKEN) { throw 'Registration token unavailable' }
    .\config.cmd --unattended --url "https://github.com/$repo" --name "manhwa-$env:COMPUTERNAME" --labels manhwa-rtx4060 --work _work
    if ($LASTEXITCODE) { throw 'Runner configuration failed' }
} finally { Remove-Item Env:\ACTIONS_RUNNER_INPUT_TOKEN -ErrorAction SilentlyContinue; Pop-Location }
