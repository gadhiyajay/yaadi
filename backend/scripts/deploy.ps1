# Builds and deploys the Yaadi backend via AWS SAM.
# Run from anywhere; always builds/deploys backend/template.yaml regardless
# of the caller's current directory. Deploy parameters (region, stack name,
# confirm_changeset, etc.) come from backend/samconfig.toml.

$ErrorActionPreference = "Stop"

Push-Location (Join-Path $PSScriptRoot "..")
try {
    sam build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    sam deploy
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
