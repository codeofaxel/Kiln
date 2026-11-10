# Kiln installer for Windows.
#
#   irm https://www.kiln3d.com/install.ps1 | iex
#
# Installs Kiln and connects it to every AI app on this computer.  Run it
# again to update.  Every change to an app's settings is backed up and
# written to a receipt at ~\.kiln\install\receipt.json, and -Uninstall takes
# back exactly what the receipt says.  With options:
#
#   & ([scriptblock]::Create((irm https://www.kiln3d.com/install.ps1))) -DryRun
#
#   -DryRun            show what would happen, change nothing
#   -Only <apps>       only these apps, comma-separated: claude-code,
#                      claude-desktop, codex, cursor, gemini, grok, copilot,
#                      devin, amp, opencode, cline, lmstudio, goose, hermes,
#                      openclaw
#   -Uninstall         take Kiln back out, exactly as the receipt says
#   -Version <x.y.z>   install this Kiln release
#
# Everything runs inside one function called on the last line, so a
# download cut short runs nothing, and an error never closes your window.

param(
    [switch]$DryRun,
    [string]$Only = "",
    [switch]$Uninstall,
    [string]$Version = "",
    [switch]$Help
)

function Install-Kiln {
    param([switch]$DryRun, [string]$Only, [switch]$Uninstall, [string]$Version, [switch]$Help)

    # The Kiln release this installer installs by default.  A test keeps it
    # equal to the package version and to install.sh.
    $KilnDefaultVersion = "1.5.1"
    $Pkg = "kiln3d"
    # The Python Kiln runs on, which uv fetches when needed: the one Kiln's
    # own tests run on, so every part Kiln depends on has a ready-made build.
    $KilnPython = "3.12"
    $ErrorActionPreference = "Stop"

    function Say([string]$text) { Write-Host $text }
    function Step([string]$text) { Write-Host ""; Write-Host $text }
    function Warn([string]$text) { Write-Host "  ! $text" -ForegroundColor Yellow }

    if ($Help) {
        Say "Install Kiln and connect it to the AI apps on this computer."
        Say "  irm https://www.kiln3d.com/install.ps1 | iex"
        Say "  & ([scriptblock]::Create((irm https://www.kiln3d.com/install.ps1))) -DryRun -Only cursor,codex -Uninstall -Version x.y.z"
        return
    }
    if (-not $Version) { $Version = $KilnDefaultVersion }
    if ($Version -notmatch '^[0-9A-Za-z.+-]+$') { Write-Host "  x not a version: $Version" -ForegroundColor Red; return }
    if ($Only -and $Only -notmatch '^[A-Za-z0-9_,-]+$') { Write-Host "  x not a list of apps: $Only" -ForegroundColor Red; return }
    # KILN_PACKAGE lets a release candidate (a wheel file) be tried before it
    # is published; normally it is the pinned release.
    $Spec = if ($env:KILN_PACKAGE) { $env:KILN_PACKAGE } else { "$Pkg==$Version" }
    $OnlyArgs = @()
    if ($Only) { $OnlyArgs = @("--only", $Only) }

    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kiln-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $saved = @{}
    try {
        if ($DryRun) {
            Say "Kiln installer, dry run. Nothing on this computer will change."
            # Anything a dry run downloads stays in a folder deleted at the end.
            foreach ($pair in @(@("UV_CACHE_DIR", "cache"), @("UV_PYTHON_INSTALL_DIR", "python"), @("UV_TOOL_DIR", "tools"))) {
                $saved[$pair[0]] = [Environment]::GetEnvironmentVariable($pair[0], "Process")
                [Environment]::SetEnvironmentVariable($pair[0], (Join-Path $tmp $pair[1]), "Process")
            }
        } else {
            Say "Kiln installer on Windows"
        }

        # --- uv: installs Kiln and the Python Kiln runs on
        $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
        if (-not $uv) {
            $local = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
            if (Test-Path $local) { $uv = $local }
        }
        if (-not $uv) {
            if ($DryRun) {
                Say "  Would install uv (it installs Kiln and the Python Kiln runs on)."
                # An unmanaged install writes no receipt and touches no PATH.
                $env:UV_UNMANAGED_INSTALL = Join-Path $tmp "uv"
                powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
                Remove-Item Env:UV_UNMANAGED_INSTALL
                $uv = Join-Path $tmp "uv\uv.exe"
            } else {
                Step "Installing uv (it installs Kiln and the Python Kiln runs on)"
                powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
                $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
            }
            if (-not (Test-Path $uv)) { throw "uv didn't install; see https://docs.astral.sh/uv" }
        }

        $binDir = (& $uv tool dir --bin).Trim()
        $kiln = Join-Path $binDir "kiln.exe"
        function Invoke-KilnCli {
            param([string[]]$KilnArgs)
            # Out-Host shows Kiln's own output instead of returning it.
            if (-not $DryRun -and (Test-Path $kiln)) { & $kiln @KilnArgs | Out-Host }
            else { & $uv tool run --quiet --python $KilnPython --from $Spec kiln @KilnArgs | Out-Host }
            return $LASTEXITCODE
        }

        if ($Uninstall) {
            Step "Taking Kiln out of your AI apps"
            $receipt = Join-Path $env:USERPROFILE ".kiln\install\receipt.json"
            if (Test-Path $receipt) {
                $a = @("uninstall-mcp", "--receipt-only") + $OnlyArgs
                if ($DryRun) { $a += "--dry-run" }
                if ((Invoke-KilnCli $a) -ne 0) { Warn "some apps weren't restored; see above" }
            } else {
                Say "  No receipt at ~\.kiln\install\receipt.json, so there are no app settings to take back."
            }
            if ($Only) { Say ""; Say "Kiln itself stays installed (you chose -Only)."; return }
            Step "Removing Kiln"
            if ($DryRun) { Say "  Would remove the Kiln program ($Pkg)." }
            elseif ((& $uv tool list 2>$null) -match "^$Pkg ") { & $uv tool uninstall $Pkg }
            else { Say "  uv doesn't list Kiln, so there's no program to remove." }
            Say ""
            Say "Your printers, prints and settings in ~\.kiln stay. Delete that folder yourself if you want them gone."
            if ($DryRun) { Say "Dry run finished. Nothing was changed." }
            return
        }

        if ($DryRun) {
            Step "Kiln $Version"
            if (Test-Path $kiln) { Say "  Would update Kiln at $kiln to $Version" }
            else { Say "  Would install Kiln $Version at $kiln" }
            Step "Your AI apps"
            if ((Invoke-KilnCli (@("install-mcp", "--dry-run", "--command", $kiln) + $OnlyArgs)) -ne 0) {
                throw "couldn't plan the AI apps"
            }
            Step "Then"
            Say "  Would offer to install OpenSCAD (Kiln's design engine) if it's missing."
            Say "  Would offer a free Kiln account, if you're not signed in."
            Say "  Would ask you to accept Kiln's terms of use, if you haven't yet."
            Say ""
            Say "Dry run finished. Nothing was changed."
            return
        }

        Step "Installing Kiln $Version"
        & $uv tool install --force --python $KilnPython $Spec
        if ($LASTEXITCODE -ne 0) { throw "couldn't install Kiln $Version" }
        if (-not (Test-Path $kiln)) { throw "Kiln installed, but $kiln is missing" }

        Step "Connecting Kiln to your AI apps"
        & $kiln install-mcp --command $kiln @OnlyArgs
        if ($LASTEXITCODE -ne 0) { Warn "some apps weren't connected; see above" }

        # A person is here when the console isn't redirected.
        $interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected

        Step "OpenSCAD (Kiln's design engine)"
        & $kiln install-openscad --check *> $null
        if ($LASTEXITCODE -eq 0) { Say "  Already installed." }
        elseif ($interactive -and ((Read-Host "  Install OpenSCAD now? Kiln designs parts with it. [Y/n]") -notmatch '^[nN]')) {
            & $kiln install-openscad
            if ($LASTEXITCODE -ne 0) { Warn "OpenSCAD didn't install; run 'kiln install-openscad' later" }
        } else { Say "  Skipped. Install it any time with: kiln install-openscad" }

        Step "Your Kiln account"
        if ($interactive) {
            & $kiln signin --offer
            if ($LASTEXITCODE -ne 0) { Warn "not signed in; run 'kiln signin' any time" }
        } else {
            Say "  Sign in any time with: kiln signin"
        }

        Step "Kiln's terms of use"
        if ($interactive) {
            # The person types their own acceptance, at the console.
            & $kiln accept-terms --if-needed
            if ($LASTEXITCODE -ne 0) { Warn "not accepted yet; run 'kiln accept-terms' when ready" }
        } else {
            Say "  There's no console here to ask you in. Run this yourself: kiln accept-terms"
        }

        Step "Done"
        if (-not (($env:PATH -split ';') -contains $binDir)) {
            Say "  Open a new window (or run: uv tool update-shell) so the 'kiln' command is found."
        }
        Say "  Restart the apps listed above, then ask one of them to design something."
        Say "  Run this again any time to update. To remove Kiln:"
        Say "    & ([scriptblock]::Create((irm https://www.kiln3d.com/install.ps1))) -Uninstall"
    } catch {
        Write-Host "  x $($_.Exception.Message)" -ForegroundColor Red
    } finally {
        foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

Install-Kiln -DryRun:$DryRun -Only $Only -Uninstall:$Uninstall -Version $Version -Help:$Help
