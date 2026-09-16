#!/usr/bin/env bash

set -euo pipefail

die() {
    echo "Error: $*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: install.sh [VERSION]

Install the SteamOS Companion Decky plugin from a GitHub release.

VERSION may be a release tag such as v0.4.0. If omitted, the latest release
is installed. VERSION can also be supplied through the environment.
DECKY_PLUGIN_DIR can be set when Decky uses a non-default plugin directory.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

[[ "${EUID}" -ne 0 ]] || die "run this script as the Steam Deck user, not root"
[[ -n "${HOME:-}" ]] || die "HOME is not set"

readonly REPOSITORY="${REPOSITORY:-tuthan/steamos-companion-decky}"
readonly PLUGIN_NAME="steamos-companion"
readonly PLUGIN_DIR="${DECKY_PLUGIN_DIR:-${HOME}/homebrew/plugins}"
readonly REQUESTED_VERSION="${1:-${VERSION:-LATEST}}"

[[ "${PLUGIN_DIR}" == /* ]] || die "Decky plugin directory must be an absolute path"
[[ "${PLUGIN_DIR}" != "/" && "${PLUGIN_DIR}" != "${HOME}" ]] || \
    die "refusing to use an unsafe Decky plugin directory: ${PLUGIN_DIR}"

for command in curl sed head awk mktemp mkdir cp rm sudo; do
    command -v "${command}" >/dev/null 2>&1 || die "required command not found: ${command}"
done

if command -v 7z >/dev/null 2>&1; then
    EXTRACTOR="7z"
elif command -v unzip >/dev/null 2>&1; then
    EXTRACTOR="unzip"
else
    die "required command not found: install 7z or unzip"
fi

if command -v sha256sum >/dev/null 2>&1; then
    CHECKSUM_TOOL="sha256sum"
else
    CHECKSUM_TOOL=""
    echo "Warning: sha256sum not found; checksum verification is unavailable" >&2
fi

[[ "${REPOSITORY}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || \
    die "invalid GitHub repository: ${REPOSITORY}"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

release_url="https://api.github.com/repos/${REPOSITORY}/releases/latest"
if [[ "${REQUESTED_VERSION}" != "LATEST" ]]; then
    release_url="https://api.github.com/repos/${REPOSITORY}/releases/tags/${REQUESTED_VERSION}"
fi

if [[ "${REQUESTED_VERSION}" == "LATEST" ]]; then
    echo "Looking up the latest release for ${REPOSITORY}"
else
    echo "Looking up ${REQUESTED_VERSION} release for ${REPOSITORY}"
fi
release_json="$(curl \
    --fail \
    --location \
    --silent \
    --show-error \
    --retry 3 \
    --header 'Accept: application/vnd.github+json' \
    --header 'X-GitHub-Api-Version: 2022-11-28' \
    --user-agent 'steamos-companion-installer' \
    "${release_url}")" || die "could not read the GitHub release"

release_tag="$(
    printf '%s\n' "${release_json}" |
        sed -nE 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' |
        head -n 1
)"
[[ -n "${release_tag}" ]] || die "GitHub release does not contain a tag"
release_version="${release_tag#v}"
[[ -n "${release_version}" && "${release_version}" != "${release_tag}" ]] || \
    die "unexpected GitHub release tag: ${release_tag}"

download_url="$(
    printf '%s\n' "${release_json}" |
        sed -nE 's/.*"browser_download_url"[[:space:]]*:[[:space:]]*"([^"[:space:]]*\/steamos-companion-decky-[^"[:space:]]+\.zip)".*/\1/p' |
        awk -v expected="steamos-companion-decky-${release_version}.zip" '
            $0 ~ "/" expected "$" { print; exit }
        '
)"
[[ -n "${download_url}" ]] || die "release does not contain a steamos-companion-decky ZIP asset"
[[ "${download_url}" == "https://github.com/${REPOSITORY}/releases/download/"* ]] || \
    die "release asset is not hosted by the configured GitHub repository"

archive_name="${download_url##*/}"
[[ "${archive_name}" == steamos-companion-decky-*.zip ]] || \
    die "unexpected release asset: ${archive_name}"

archive_path="${tmp_dir}/${archive_name}"
checksum_path="${tmp_dir}/${archive_name}.sha256"

echo "Downloading ${archive_name}"
curl \
    --fail \
    --location \
    --silent \
    --show-error \
    --retry 3 \
    "${download_url}" \
    --output "${archive_path}" || die "could not download ${archive_name}"

if [[ -n "${CHECKSUM_TOOL}" ]]; then
    if curl \
        --fail \
        --location \
        --silent \
        --show-error \
        --retry 3 \
        "${download_url}.sha256" \
        --output "${checksum_path}"; then
        expected_checksum="$(awk 'NF { print $1; exit }' "${checksum_path}")"
        actual_checksum="$("${CHECKSUM_TOOL}" "${archive_path}" | awk '{ print $1 }')"
        [[ "${expected_checksum}" =~ ^[[:xdigit:]]{64}$ ]] || die "release checksum is malformed"
        [[ "${expected_checksum,,}" == "${actual_checksum,,}" ]] || die "release checksum does not match"
        echo "SHA256 checksum verified"
    else
        echo "Warning: no checksum asset found; continuing without verification" >&2
    fi
fi

staging_dir="${tmp_dir}/staging"
mkdir -p "${staging_dir}"
echo "Extracting plugin"
if [[ "${EXTRACTOR}" == "7z" ]]; then
    7z x -y "${archive_path}" "-o${staging_dir}" >/dev/null
else
    unzip -q "${archive_path}" -d "${staging_dir}"
fi

staged_plugin="${staging_dir}/${PLUGIN_NAME}"
[[ -d "${staged_plugin}" ]] || die "archive does not contain ${PLUGIN_NAME}/"
[[ -f "${staged_plugin}/plugin.json" ]] || die "archive is missing ${PLUGIN_NAME}/plugin.json"
[[ -f "${staged_plugin}/main.py" ]] || die "archive is missing ${PLUGIN_NAME}/main.py"

echo "Installing into ${PLUGIN_DIR}/${PLUGIN_NAME}"
sudo -v || die "sudo authorization is required to install the plugin"
sudo mkdir -p "${PLUGIN_DIR}" || die "cannot create Decky plugin directory: ${PLUGIN_DIR}"
sudo rm -rf "${PLUGIN_DIR}/${PLUGIN_NAME}" || die "cannot remove the previous plugin installation"
sudo cp -a "${staged_plugin}" "${PLUGIN_DIR}/${PLUGIN_NAME}" || die "cannot copy the plugin into ${PLUGIN_DIR}"

echo "Requesting Decky plugin reload"
if curl --fail --silent --show-error --max-time 5 \
    http://127.0.0.1:1337/plugins/reload >/dev/null 2>&1; then
    echo "Decky plugins reloaded"
else
    echo "Plugin installed. Use Decky's Reload Plugins action to load it."
fi

echo "Installation complete"
