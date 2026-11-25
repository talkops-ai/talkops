# Copyright talkops.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

<#
.SYNOPSIS
    Install TalkOps - AI DevOps & Cloud Operations Agent on Windows (PowerShell).
.DESCRIPTION
    Standalone native Windows installer for TalkOps. Automatically provisions
    uv, Python 3.12+, ripgrep, and the talkops / ops CLI binaries without
    requiring WSL or administrator/sudo privileges.
.EXAMPLE
    irm https://raw.githubusercontent.com/talkops-ai/talkops/main/scripts/install.ps1 | iex
.EXAMPLE
    powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/talkops-ai/talkops/main/scripts/install.ps1 | iex"
.EXAMPLE
    .\scripts\install.ps1 -Upgrade
.EXAMPLE
    .\scripts\install.ps1 -Version 0.3.0
.EXAMPLE
    .\scripts\install.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [Alias("h")]
    [switch]$Help,

    [Alias("v")]
    [switch]$PrintVersion,

    [Alias("y")]
    [switch]$Yes,

    [Alias("f", "U")]
    [switch]$Force,

    [switch]$Upgrade,
    [switch]$Reinstall,
    [switch]$Uninstall,

    [ValidateSet("", "pypi", "git", "local")]
    [string]$Source = "",

    [switch]$Git,

    [Alias("Editable")]
    [switch]$Local,

    [string]$Python = "",
    [string]$Extras = "",

    [Parameter(Position = 0)]
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-TalkOpsInstaller {
    param(
        [bool]$HelpFlag,
        [bool]$PrintVersionFlag,
        [bool]$AssumeYesFlag,
        [bool]$ForceReinstallFlag,
        [bool]$UninstallFlag,
        [string]$RequestedSource,
        [string]$RequestedPython,
        [string]$RequestedExtras,
        [string]$RequestedVersion,
        [bool]$VerboseFlag
    )

    # Ensure TLS 1.2+ on Windows PowerShell 5.1
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    }
    catch {
        # Best effort; PowerShell 7+ negotiates modern TLS automatically.
    }

    # Package & repository constants
    $PackageName = if ($env:TALKOPS_PACKAGE_NAME) { $env:TALKOPS_PACKAGE_NAME } else { "talkops-ai" }
    $PrimaryBin = "talkops"
    $AliasBin = "ops"
    $RepoUrl = if ($env:TALKOPS_REPO_URL) { $env:TALKOPS_REPO_URL } else { "https://github.com/talkops-ai/talkops" }
    $DefaultPython = "3.12"
    $RipgrepVersion = "14.1.1"
    $RequestedPrerelease = if ($env:TALKOPS_PRERELEASE) { $env:TALKOPS_PRERELEASE } else { "allow" }
    $RipgrepInstaller = if ($env:TALKOPS_RIPGREP_INSTALLER) { $env:TALKOPS_RIPGREP_INSTALLER } else { "managed" }
    $OfflineMode = ($env:TALKOPS_OFFLINE -eq "1")
    $NoModifyPath = ($env:TALKOPS_NO_MODIFY_PATH -eq "1")
    $SkipDevopsCheck = ($env:TALKOPS_SKIP_DEVOPS_CHECK -eq "1")

    if (-not $RequestedPython) {
        $RequestedPython = if ($env:TALKOPS_PYTHON) { $env:TALKOPS_PYTHON } else { $DefaultPython }
    }
    if (-not $RequestedExtras -and $env:TALKOPS_EXTRAS) {
        $RequestedExtras = $env:TALKOPS_EXTRAS
    }
    if (-not $RequestedVersion -and $env:TALKOPS_VERSION) {
        $RequestedVersion = $env:TALKOPS_VERSION
    }
    if (-not $RequestedSource -and $env:TALKOPS_SOURCE) {
        $RequestedSource = $env:TALKOPS_SOURCE
    }
    if (-not $AssumeYesFlag -and ($env:TALKOPS_YES -eq "1" -or $env:TALKOPS_YES -eq "true")) {
        $AssumeYesFlag = $true
    }
    if (-not $VerboseFlag -and $env:TALKOPS_VERBOSE -eq "1") {
        $VerboseFlag = $true
    }

    function Write-Info([string]$Message) {
        Write-Host $Message
    }

    function Write-Step([string]$Message) {
        Write-Host ""
        Write-Host "==> " -ForegroundColor Blue -NoNewline
        Write-Host $Message -ForegroundColor White
    }

    function Write-Ok([string]$Message) {
        Write-Host "[OK] " -ForegroundColor Green -NoNewline
        Write-Host $Message
    }

    function Write-WarnMsg([string]$Message) {
        Write-Host "Warning: " -ForegroundColor Yellow -NoNewline
        Write-Host $Message
    }

    function Write-ErrMsg([string]$Message) {
        Write-Host "Error: " -ForegroundColor Red -NoNewline
        Write-Host $Message
    }

    function Write-Dbg([string]$Message) {
        if ($VerboseFlag) {
            Write-Host "[DEBUG] $Message" -ForegroundColor DarkGray
        }
    }

    function Invoke-ExternalCommand {
        param(
            [Parameter(Mandatory = $true)]
            [string]$FilePath,
            [string[]]$Arguments = @(),
            [switch]$StreamOutput
        )

        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $global:LASTEXITCODE = 0
            if ($StreamOutput) {
                & $FilePath @Arguments
                $code = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }
                return @{ ExitCode = $code; Output = @() }
            }
            else {
                $rawOut = @(& $FilePath @Arguments 2>&1)
                $code = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }
                $lines = @($rawOut | ForEach-Object { $_.ToString() })
                return @{ ExitCode = $code; Output = $lines }
            }
        }
        finally {
            $ErrorActionPreference = $prevEap
        }
    }

    function Resolve-LatestPypiVersion {
        $latest = ""
        if (-not $OfflineMode) {
            try {
                $pypiUrl = "https://pypi.org/pypi/$PackageName/json"
                $resp = Invoke-RestMethod -Uri $pypiUrl -TimeoutSec 4 -ErrorAction Stop
                if ($null -ne $resp -and $resp.PSObject.Properties["info"] -and $resp.info.PSObject.Properties["version"]) {
                    $latest = [string]$resp.info.version
                }
            }
            catch {
                Write-Dbg "PyPI version lookup failed: $_"
            }
        }
        if (-not $latest) {
            $latest = "0.3.0"
        }
        return $latest.Trim()
    }

    function Show-Help {
        Write-Host "TalkOps Windows Installer - AI DevOps & Cloud Operations Agent" -ForegroundColor White
        Write-Host ""
        Write-Host "Usage:" -ForegroundColor White
        Write-Host "  irm https://raw.githubusercontent.com/talkops-ai/talkops/main/scripts/install.ps1 | iex"
        Write-Host "  powershell -ExecutionPolicy ByPass -c `"irm https://raw.githubusercontent.com/talkops-ai/talkops/main/scripts/install.ps1 | iex`""
        Write-Host "  .\scripts\install.ps1 [options]"
        Write-Host ""
        Write-Host "Options:" -ForegroundColor White
        Write-Host "  -Help, -h           Show this help message and exit"
        Write-Host "  -PrintVersion, -v   Print installer version and exit"
        Write-Host "  -Yes, -y            Accept all prompts (non-interactive)"
        Write-Host "  -Verbose            Enable verbose output and debug logs"
        Write-Host "  -Source <src>       Install source: pypi, git, or local (default: pypi)"
        Write-Host "  -Git                Force installation from GitHub repository"
        Write-Host "  -Local, -Editable   Force editable installation from local directory"
        Write-Host "  -Python <ver>       Python version to provision via uv (default: $DefaultPython)"
        Write-Host "  -Extras <extras>    Comma-separated pip extras (e.g. dev, sandboxes)"
        Write-Host "  -Version <ver>      Exact version to install (e.g. 0.3.0)"
        Write-Host "  -Force, -Upgrade    Force clean reinstall or upgrade to latest version"
        Write-Host "  -Reinstall          Reinstall package and refresh all dependencies"
        Write-Host "  -Uninstall          Uninstall talkops and remove binaries"
        Write-Host ""
        Write-Host "Environment Variables:" -ForegroundColor White
        Write-Host "  TALKOPS_VERSION           Exact version to install (e.g. 0.3.0)"
        Write-Host "  TALKOPS_SOURCE            pypi, git, or local (default: pypi)"
        Write-Host "  TALKOPS_EXTRAS            Comma-separated pip extras"
        Write-Host "  TALKOPS_PRERELEASE        uv pre-release strategy (default: allow)"
        Write-Host "  TALKOPS_PYTHON            Python version to use (default: $DefaultPython)"
        Write-Host "  TALKOPS_YES               Set to 1 to accept prompts without asking"
        Write-Host "  TALKOPS_RIPGREP_INSTALLER managed (default), system, or skip"
        Write-Host "  TALKOPS_OFFLINE           Set to 1 to skip external downloads"
        Write-Host "  TALKOPS_SKIP_DEVOPS_CHECK Set to 1 to bypass DevOps CLI checks"
        Write-Host "  TALKOPS_NO_MODIFY_PATH    Set to 1 to skip User PATH modifications"
        Write-Host "  TALKOPS_VERBOSE           Set to 1 for verbose debug logs"
        Write-Host "  UV_BIN                    Path to custom uv.exe binary"
        Write-Host ""
        Write-Host "Note: Do not run curl ... | bash in Windows cmd.exe or prefix with sudo." -ForegroundColor Yellow
        Write-Host "      For WSL2 users, open your WSL Linux terminal first and run install.sh there."
        Write-Host ""
        Write-Host "Repository & Docs: $RepoUrl"
    }

    if ($HelpFlag) {
        Show-Help
        return
    }

    if ($PrintVersionFlag) {
        Write-Output ("talkops-installer {0}" -f (Resolve-LatestPypiVersion))
        return
    }

    function Read-PromptYesNo {
        param(
            [string]$PromptText,
            [string]$DefaultAnswer = "y"
        )

        if ($AssumeYesFlag) {
            return $true
        }

        $suffix = if ($DefaultAnswer -eq "y") { "[Y/n]" } else { "[y/N]" }
        try {
            $choice = Read-Host "$PromptText $suffix"
        }
        catch {
            $choice = $DefaultAnswer
        }

        if (-not $choice) {
            $choice = $DefaultAnswer
        }
        $choice = $choice.Trim().ToLowerInvariant()
        return ($choice -eq "y" -or $choice -eq "yes")
    }

    function Update-SessionPath {
        $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $parts = @()
        foreach ($segment in @($userPath, $machinePath, $env:Path)) {
            if ($segment) {
                foreach ($entry in ($segment -split ";")) {
                    $trimmed = $entry.Trim()
                    if ($trimmed -and ($parts -notcontains $trimmed)) {
                        $parts += $trimmed
                    }
                }
            }
        }
        if ($parts.Count -gt 0) {
            $env:Path = ($parts -join ";")
        }
    }

    function Resolve-UvBin {
        if ($env:UV_BIN -and (Test-Path -LiteralPath $env:UV_BIN -PathType Leaf)) {
            return $env:UV_BIN
        }

        $cmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($null -ne $cmd -and $cmd.Source -and (Test-Path -LiteralPath $cmd.Source -PathType Leaf)) {
            return $cmd.Source
        }

        $candidates = @()
        if ($env:USERPROFILE) {
            $candidates += (Join-Path $env:USERPROFILE ".local\bin\uv.exe")
            $candidates += (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe")
            $candidates += (Join-Path $env:USERPROFILE "AppData\Local\Programs\uv\uv.exe")
        }
        if ($env:LOCALAPPDATA) {
            $candidates += (Join-Path $env:LOCALAPPDATA "Programs\uv\uv.exe")
            $candidates += (Join-Path $env:LOCALAPPDATA "uv\uv.exe")
        }

        foreach ($cand in $candidates) {
            if ($cand -and (Test-Path -LiteralPath $cand -PathType Leaf)) {
                return $cand
            }
        }
        return ""
    }

    function Install-Uv {
        if ($OfflineMode) {
            Write-ErrMsg "uv is not installed and TALKOPS_OFFLINE=1 prevents automatic download."
            throw "uv binary not found in offline mode."
        }

        Write-Step "Installing uv (high-performance Python package & environment manager)..."
        try {
            $uvInstaller = Invoke-RestMethod -Uri "https://astral.sh/uv/install.ps1" -ErrorAction Stop
            $prevEap = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            try {
                & ([scriptblock]::Create($uvInstaller))
            }
            finally {
                $ErrorActionPreference = $prevEap
            }
        }
        catch {
            Write-ErrMsg "Failed to run uv installer: $_"
        }

        Update-SessionPath
        $resolved = Resolve-UvBin
        if ($resolved) {
            Write-Ok "uv installed successfully at $resolved"
            return $resolved
        }

        Write-ErrMsg "Failed to install uv automatically."
        Write-Info "Please install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and re-run this script."
        throw "Failed to install uv."
    }

    function Resolve-ToolBinDir([string]$UvBin) {
        $dir = ""
        if ($env:UV_TOOL_BIN_DIR) {
            $dir = $env:UV_TOOL_BIN_DIR.Trim()
        }
        else {
            $res = Invoke-ExternalCommand -FilePath $UvBin -Arguments @("tool", "dir", "--bin")
            if ($res.ExitCode -eq 0 -and @($res.Output).Count -gt 0) {
                $candidate = (@($res.Output) | Where-Object { $_.Trim() -ne "" } | Select-Object -Last 1)
                if ($candidate) {
                    $dir = $candidate.Trim()
                }
            }
        }

        if (-not $dir) {
            $homeDir = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
            $localBin = Join-Path $homeDir ".local\bin"
            $cargoBin = Join-Path $homeDir ".cargo\bin"
            if (Test-Path -LiteralPath $localBin -PathType Container) {
                $dir = $localBin
            }
            elseif (Test-Path -LiteralPath $cargoBin -PathType Container) {
                $dir = $cargoBin
            }
            else {
                $dir = $localBin
            }
        }

        if (-not (Test-Path -LiteralPath $dir -PathType Container)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }

        Write-Dbg "Tool binary directory resolved to: $dir"
        return $dir
    }

    function Install-Ripgrep([string]$ToolBinDir) {
        if ($RipgrepInstaller -eq "skip" -or $OfflineMode) {
            return
        }

        if (Get-Command rg -ErrorAction SilentlyContinue) {
            Write-Dbg "ripgrep is already installed on PATH."
            return
        }

        $targetRg = Join-Path $ToolBinDir "rg.exe"
        if (Test-Path -LiteralPath $targetRg -PathType Leaf) {
            Write-Dbg "ripgrep already present at $targetRg"
            return
        }

        if ($RipgrepInstaller -eq "system") {
            Write-Info "ripgrep not found. To install via Windows package manager:"
            Write-Info "  winget install BurntSushi.ripgrep.MSVC"
            return
        }

        $asset = "ripgrep-$RipgrepVersion-x86_64-pc-windows-msvc.zip"
        $rgUrl = "https://github.com/BurntSushi/ripgrep/releases/download/$RipgrepVersion/$asset"
        Write-Step "Provisioning ripgrep $RipgrepVersion (fast code & file search)..."

        $tmpDir = Join-Path ([IO.Path]::GetTempPath()) ("talkops-rg-" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
        [void]$tempDirs.Add($tmpDir)

        try {
            $archivePath = Join-Path $tmpDir $asset
            Invoke-WebRequest -Uri $rgUrl -OutFile $archivePath -UseBasicParsing -ErrorAction Stop
            Expand-Archive -Path $archivePath -DestinationPath $tmpDir -Force
            $foundRg = Get-ChildItem -Path $tmpDir -Filter "rg.exe" -Recurse -File | Select-Object -First 1
            if ($null -ne $foundRg) {
                Copy-Item -LiteralPath $foundRg.FullName -Destination $targetRg -Force
                Write-Ok "ripgrep provisioned in $targetRg"
                return
            }
        }
        catch {
            Write-Dbg "Automatic ripgrep download failed: $_"
        }

        Write-Dbg "Could not automatically download ripgrep. TalkOps will use built-in Python search."
    }

    function Test-DevopsPrerequisites {
        if ($SkipDevopsCheck) {
            return
        }
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            Write-WarnMsg "git is not installed. TalkOps relies on git for repository bounds and plugin management."
            Write-Info "  To install git: winget install Git.Git"
        }
    }

    function Add-ToUserPath([string]$BinDir) {
        $normalizedTarget = $BinDir.TrimEnd("\", "/")

        # Update current PowerShell session PATH
        $sessionEntries = @($env:Path -split ";" | Where-Object { $_ -and $_.Trim() -ne "" })
        $inSession = $false
        foreach ($entry in $sessionEntries) {
            if ($entry.Trim().TrimEnd("\", "/").Equals($normalizedTarget, [StringComparison]::OrdinalIgnoreCase)) {
                $inSession = $true
                break
            }
        }
        if (-not $inSession) {
            $env:Path = "$BinDir;$env:Path"
        }

        if ($NoModifyPath) {
            return $inSession
        }

        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $userEntries = @()
        if ($userPath) {
            $userEntries = @($userPath -split ";" | Where-Object { $_ -and $_.Trim() -ne "" })
        }

        $inUserPath = $false
        foreach ($entry in $userEntries) {
            if ($entry.Trim().TrimEnd("\", "/").Equals($normalizedTarget, [StringComparison]::OrdinalIgnoreCase)) {
                $inUserPath = $true
                break
            }
        }

        if (-not $inUserPath) {
            $newUserPath = if ($userPath) { "$BinDir;$userPath" } else { $BinDir }
            [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
            Write-Ok "Added $BinDir to your Windows User PATH."
            return $false
        }

        return $true
    }

    $tempDirs = New-Object "System.Collections.Generic.List[string]"

    try {
        # Step 1: Ensure uv is available
        $uvBin = Resolve-UvBin
        if (-not $uvBin) {
            $uvBin = Install-Uv
        }
        Write-Dbg "Using uv binary: $uvBin"

        # Step 2: Resolve tool binary directory
        $toolBinDir = Resolve-ToolBinDir -UvBin $uvBin

        # Step 3: Handle Uninstall
        if ($UninstallFlag) {
            Write-Step "Uninstalling TalkOps..."
            $unRes = Invoke-ExternalCommand -FilePath $uvBin -Arguments @("tool", "uninstall", $PackageName)
            if ($unRes.ExitCode -eq 0) {
                Write-Ok "Removed $PackageName uv tool environment."
            }
            else {
                Write-Info "No active uv tool installation found for $PackageName."
            }

            foreach ($binName in @("$PrimaryBin.exe", $PrimaryBin, "$AliasBin.exe", $AliasBin)) {
                $binPath = Join-Path $toolBinDir $binName
                if (Test-Path -LiteralPath $binPath) {
                    Remove-Item -LiteralPath $binPath -Force -ErrorAction SilentlyContinue
                    Write-Info "Removed $binPath"
                }
            }

            Write-Host ""
            $talkopsHome = Join-Path (if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }) ".talkops"
            if (Read-PromptYesNo -PromptText "Do you also want to remove TalkOps configuration and state ($talkopsHome)?" -DefaultAnswer "n") {
                if (Test-Path -LiteralPath $talkopsHome) {
                    Remove-Item -LiteralPath $talkopsHome -Recurse -Force -ErrorAction SilentlyContinue
                }
                Write-Ok "Removed $talkopsHome data directory."
            }
            else {
                Write-Info "Preserved user configuration in $talkopsHome."
            }

            Write-Host ""
            Write-Ok "TalkOps has been uninstalled."
            Write-Host ""
            return
        }

        # Step 4: Detect existing installation
        $existingCmd = Get-Command $PrimaryBin -ErrorAction SilentlyContinue
        if ($null -ne $existingCmd) {
            $verRes = Invoke-ExternalCommand -FilePath $existingCmd.Source -Arguments @("--version")
            if ($verRes.ExitCode -eq 0 -and @($verRes.Output).Count -gt 0) {
                $tokens = @((@($verRes.Output)[0].Trim()) -split "\s+")
                $currentVer = if ($tokens.Count -gt 0) { $tokens[$tokens.Count - 1] } else { "" }
                if ($currentVer) {
                    Write-Info "Existing TalkOps $currentVer detected."
                    if ($ForceReinstallFlag) {
                        Write-Info "Removing previous TalkOps ($currentVer) before clean upgrade..."
                        [void](Invoke-ExternalCommand -FilePath $uvBin -Arguments @("tool", "uninstall", $PackageName))
                    }
                }
            }
        }

        # Step 5: Provision ripgrep & check DevOps prerequisites
        Install-Ripgrep -ToolBinDir $toolBinDir
        Test-DevopsPrerequisites

        # Step 6: Install TalkOps via uv tool
        $resolvedSource = if ($RequestedSource -in @("pypi", "git", "local")) { $RequestedSource } else { "pypi" }

        function Install-TalkOpsPackage([string]$TargetSource) {
            $installArgs = New-Object "System.Collections.Generic.List[string]"
            $installArgs.Add("tool")
            $installArgs.Add("install")
            $installArgs.Add("--python")
            $installArgs.Add($RequestedPython)
            $installArgs.Add("--force")
            $installArgs.Add("--upgrade")
            $installArgs.Add("--refresh")

            if ($ForceReinstallFlag) {
                $installArgs.Add("--reinstall")
            }
            if ($RequestedPrerelease) {
                $installArgs.Add("--prerelease=$RequestedPrerelease")
            }

            $displayTarget = ""
            if ($TargetSource -eq "local") {
                $displayTarget = "TalkOps (local editable: .)"
                $installArgs.Add("--editable")
                $installArgs.Add(".")
                if ($RequestedExtras) {
                    $installArgs.Add("--extra")
                    $installArgs.Add($RequestedExtras)
                }
            }
            elseif ($TargetSource -eq "git") {
                $gitSpec = "$RepoUrl.git"
                if ($RequestedVersion) {
                    $gitSpec = "$RepoUrl.git@$RequestedVersion"
                }
                $displayTarget = "TalkOps from GitHub ($gitSpec)"
                $fullSpec = if ($RequestedExtras) {
                    "${PackageName}[${RequestedExtras}] @ git+${gitSpec}"
                }
                else {
                    "${PackageName} @ git+${gitSpec}"
                }
                $installArgs.Add($fullSpec)
            }
            else {
                $spec = $PackageName
                if ($RequestedExtras) {
                    $spec = "${spec}[${RequestedExtras}]"
                }
                if ($RequestedVersion) {
                    $pypiVer = $RequestedVersion.TrimStart("v")
                    $spec = "${spec}==${pypiVer}"
                    $displayTarget = "$spec from PyPI"
                }
                else {
                    $latestPypi = Resolve-LatestPypiVersion
                    $displayTarget = "$spec ($latestPypi) from PyPI"
                }
                $installArgs.Add($spec)
            }

            Write-Step "Installing $displayTarget via uv tool..."
            $argArray = $installArgs.ToArray()

            if ($VerboseFlag) {
                $res = Invoke-ExternalCommand -FilePath $uvBin -Arguments $argArray -StreamOutput
                if ($res.ExitCode -ne 0) {
                    if ($TargetSource -eq "pypi" -and (-not $RequestedSource) -and (-not $OfflineMode)) {
                        Write-WarnMsg "PyPI resolution for '$PackageName' did not succeed."
                        Write-Info "Falling back to installing directly from GitHub repository ($RepoUrl)..."
                        Install-TalkOpsPackage -TargetSource "git"
                        return
                    }
                    throw "uv tool install failed with exit code $($res.ExitCode)."
                }
            }
            else {
                $res = Invoke-ExternalCommand -FilePath $uvBin -Arguments $argArray
                if ($res.ExitCode -ne 0) {
                    if ($TargetSource -eq "pypi" -and (-not $RequestedSource) -and (-not $OfflineMode)) {
                        Write-WarnMsg "PyPI resolution for '$PackageName' did not succeed."
                        Write-Info "Falling back to installing directly from GitHub repository ($RepoUrl)..."
                        Install-TalkOpsPackage -TargetSource "git"
                        return
                    }
                    Write-ErrMsg "Installation command failed:"
                    foreach ($line in @($res.Output)) {
                        Write-Host $line -ForegroundColor Red
                    }
                    throw "uv tool install failed with exit code $($res.ExitCode)."
                }
            }

            Write-Ok "TalkOps package installed successfully."
        }

        Install-TalkOpsPackage -TargetSource $resolvedSource

        # Step 7: Ensure PATH is configured
        $alreadyInPath = Add-ToUserPath -BinDir $toolBinDir

        # Step 8: Smoke test installed binary
        $installedPrimary = ""
        foreach ($candidate in @((Join-Path $toolBinDir "$PrimaryBin.exe"), (Join-Path $toolBinDir $PrimaryBin))) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $installedPrimary = $candidate
                break
            }
        }
        if (-not $installedPrimary) {
            $cmdCheck = Get-Command $PrimaryBin -ErrorAction SilentlyContinue
            if ($null -ne $cmdCheck) {
                $installedPrimary = $cmdCheck.Source
            }
        }

        $finalVersion = ""
        if ($installedPrimary) {
            $vCheck = Invoke-ExternalCommand -FilePath $installedPrimary -Arguments @("--version")
            if ($vCheck.ExitCode -eq 0 -and @($vCheck.Output).Count -gt 0) {
                $parts = (@($vCheck.Output)[0].Trim() -split "\s+")
                if ($parts.Count -gt 0) {
                    $finalVersion = $parts[$parts.Count - 1]
                }
            }
        }

        # Step 9: Welcome banner & quick start (pure ASCII for PowerShell 5.1 safety)
        Write-Host ""
        Write-Host "  +==========================================================+" -ForegroundColor Cyan
        Write-Host "  |   TALKOPS                                                |" -ForegroundColor Cyan
        Write-Host "  |   Autonomous AI Agent for DevOps & Cloud Operations      |" -ForegroundColor Cyan
        Write-Host "  +==========================================================+" -ForegroundColor Cyan
        Write-Host ""

        if ($finalVersion) {
            Write-Ok "TalkOps $finalVersion installed successfully!"
        }
        else {
            Write-Ok "TalkOps installed successfully!"
        }
        Write-Host ""

        if ($alreadyInPath) {
            Write-Info "Launch interactive session (using talkops or ops alias):"
            Write-Host "  talkops   # or: ops" -ForegroundColor White
        }
        else {
            Write-Host "Note: " -ForegroundColor Yellow -NoNewline
            Write-Host "$toolBinDir has been added to your current session and User PATH."
            Write-Info "Open a new PowerShell window (or run right now in this window):"
            Write-Host "  talkops   # or: ops" -ForegroundColor White
        }

        Write-Host ""
        Write-Host "Quick Commands:" -ForegroundColor White
        Write-Host "  talkops auth login --sso   Sign in with free Browser SSO" -ForegroundColor Cyan
        Write-Host "  talkops auth set anthropic Configure LLM provider API key (or use -M ollama:...)" -ForegroundColor Cyan
        Write-Host "  talkops doctor             Verify AWS/K8s/Terraform toolchain & credentials" -ForegroundColor Cyan
        Write-Host "  talkops -p `"<prompt>`"      Run non-interactive / headless operational task" -ForegroundColor Cyan
        Write-Host "  talkops serve              Start the centralized multi-client & webhook server" -ForegroundColor Cyan
        Write-Host "  ops --help                 Show full CLI documentation and subcommands" -ForegroundColor Cyan
        Write-Host ""
    }
    finally {
        foreach ($dir in $tempDirs) {
            if ($dir -and (Test-Path -LiteralPath $dir)) {
                Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

$effectiveSource = $Source
if ($Git) {
    $effectiveSource = "git"
}
elseif ($Local) {
    $effectiveSource = "local"
}

$effectiveForce = [bool]($Force -or $Upgrade -or $Reinstall)
$effectiveVerbose = ($VerbosePreference -ne "SilentlyContinue")

Invoke-TalkOpsInstaller `
    -HelpFlag ([bool]$Help) `
    -PrintVersionFlag ([bool]$PrintVersion) `
    -AssumeYesFlag ([bool]$Yes) `
    -ForceReinstallFlag $effectiveForce `
    -UninstallFlag ([bool]$Uninstall) `
    -RequestedSource $effectiveSource `
    -RequestedPython $Python `
    -RequestedExtras $Extras `
    -RequestedVersion $Version `
    -VerboseFlag $effectiveVerbose
