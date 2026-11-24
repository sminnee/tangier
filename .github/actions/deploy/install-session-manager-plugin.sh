#!/bin/bash
# Install the AWS Session Manager plugin at SSM_PLUGIN_VERSION, for an aws-ssm
# deploy's port-forward. A runner that already has it, as GitHub's Ubuntu
# images do, keeps its own.
#
# Pinned by version, never `latest`, and unpacked into $RUNNER_TEMP rather than
# installed with dpkg, as install-regctl.sh does: no root needed.
set -euo pipefail
if command -v session-manager-plugin >/dev/null; then
  echo "session-manager-plugin $(session-manager-plugin --version) is already installed"
  exit 0
fi
if [ "$(uname -s)" != Linux ]; then
  echo "::error::no Session Manager plugin install for $(uname -s) runners; install it in an earlier step" >&2
  exit 1
fi
case "$(uname -m)" in
  x86_64) arch=ubuntu_64bit ;;
  aarch64 | arm64) arch=ubuntu_arm64 ;;
  *)
    echo "::error::no Session Manager plugin build for this runner's architecture: $(uname -m)" >&2
    exit 1
    ;;
esac
dest="$RUNNER_TEMP/session-manager-plugin"
mkdir -p "$dest"
curl -fsSL -o "$dest/plugin.deb" \
  "https://s3.amazonaws.com/session-manager-downloads/plugin/${SSM_PLUGIN_VERSION}/${arch}/session-manager-plugin.deb"
dpkg-deb -x "$dest/plugin.deb" "$dest"
bin="$dest/usr/local/sessionmanagerplugin/bin"
echo "$bin" >>"$GITHUB_PATH"
"$bin/session-manager-plugin" --version
