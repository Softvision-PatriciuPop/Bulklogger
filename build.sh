#!/usr/bin/env bash
# Builds dist/Bulklogger.app on macOS (and a plain binary on Linux).
#
#     python3 -m pip install -r requirements-dev.txt
#     ./build.sh
#
# PyInstaller cannot cross-compile: this has to run on the OS you are
# targeting. To build for macOS without a Mac, push and let
# .github/workflows/build.yml do it.
#
# tickets.toml and credentials.toml are deliberately NOT bundled. The app reads
# them from the folder containing Bulklogger.app, so each person keeps their own
# token beside their own copy and a rebuild never overwrites either.

set -euo pipefail
cd "$(dirname "$0")"

if ! command -v pyinstaller >/dev/null 2>&1; then
    echo "pyinstaller not found. Run: python3 -m pip install -r requirements-dev.txt" >&2
    exit 1
fi

case "$(uname -s)" in
    Darwin) ICON=(--icon bulklogger.icns); TARGET="dist/Bulklogger.app" ;;
    *)      ICON=();                       TARGET="dist/Bulklogger" ;;
esac

echo "Building ${TARGET}..."

# note the ':' data separator - Windows uses ';'
pyinstaller \
    --noconfirm \
    --clean \
    --onefile \
    --windowed \
    --name Bulklogger \
    "${ICON[@]}" \
    --add-data "bulklogger.png:." \
    --add-data "bulklogger.ico:." \
    --collect-all tzdata \
    --hidden-import theme \
    bulklogger.py

# The app aborts without a tickets.toml beside it, so put the shared one there
# and dist/ is immediately runnable. It holds no secrets.
cp tickets.toml dist/

# Running the app from dist/ writes personal state next to it, and dist/ is the
# folder people zip up. Strip it, loudly, so a token cannot ride along.
removed=()
for f in credentials.toml draft.json usage.json; do
    if [ -e "dist/$f" ]; then rm -f "dist/$f"; removed+=("$f"); fi
done
if [ ${#removed[@]} -gt 0 ]; then
    echo
    echo "REMOVED personal files from dist/ before packaging: ${removed[*]}"
    echo "  credentials.toml holds YOUR API token - it must never be shared."
    echo "  If you have already sent out a build, revoke that token at"
    echo "  id.atlassian.com -> Security -> API tokens."
fi

echo
echo "Built ${TARGET}"
echo
echo "To distribute, ship exactly these two:"
echo "    ${TARGET}"
echo "    dist/tickets.toml"
echo "Each person runs it once and enters their own API token."
echo
echo "Do NOT ship credentials.toml, draft.json or usage.json."

if [ "$(uname -s)" = "Darwin" ]; then
    echo
    echo "Built for $(lipo -archs "$TARGET/Contents/MacOS/Bulklogger")."
    echo "PyInstaller cannot cross-compile, so this app only runs on Macs with"
    echo "the same chip as this one. Recipients on the other chip get"
    echo "\"not supported on this Mac\" - build there too, or use the workflow,"
    echo "which builds both."
    echo
    echo "The .app is unsigned, so Gatekeeper will block a double-click. On"
    echo "macOS 15+ right-click -> Open no longer works; the first launch has to"
    echo "be approved in System Settings -> Privacy & Security -> Open Anyway."
    echo "Simpler, if you trust the sender:"
    echo "    xattr -dr com.apple.quarantine /path/to/Bulklogger.app"
fi
