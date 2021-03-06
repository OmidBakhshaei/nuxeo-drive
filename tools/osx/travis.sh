#!/bin/bash

set -e

# Certificates
wget https://www.apple.com/appleca/AppleIncRootCertificate.cer
echo "${CERT_APP_MACOS}" | base64 --decode > developerID_application.cer
echo "${PRIV_APP_MACOS}" | base64 --decode > nuxeo-drive.priv

# Install required stuff
bash tools/osx/deploy_ci_agent.sh --install-release

# Test the auto-updater
rm -rf build dist
bash tools/osx/deploy_ci_agent.sh --check-upgrade

# Upload artifacts
for f in dist/*.dmg; do
    bash tools/upload.sh staging "${f}"
done
