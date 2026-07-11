#!/usr/bin/env bash
# v4.1 T1.4 — pre-provision Headroom's runtime assets for no-egress use.
# Run on an EGRESS-CAPABLE host; the resulting directory is mounted
# read-only into the headroom service (deploy/compose.yaml profile
# `headroom`; deploy/k8s/headroom.yaml).
#
# Headroom fetches two things over TLS on first use (DR-3):
#   1. ONNX runtime (cdn.pyke.io)      -> ORT_STRATEGY=system + ORT_LIB_LOCATION
#   2. Kompress-v2-base (huggingface)  -> HF_HOME cache + HF_HUB_OFFLINE=1
# Everything else (SmartCrusher etc.) is local and needs no assets.
set -euo pipefail

ASSETS_DIR="${ASSETS_DIR:-$(pwd)/deploy/headroom-assets}"
mkdir -p "$ASSETS_DIR/ort" "$ASSETS_DIR/hf"

echo "==> downloading Kompress-v2-base into $ASSETS_DIR/hf"
pip install --quiet --only-binary :all: "huggingface_hub" >/dev/null
HF_HOME="$ASSETS_DIR/hf" python - <<'EOF'
from huggingface_hub import snapshot_download
snapshot_download("chopratejas/kompress-v2-base")
print("model cached")
EOF

echo "==> provisioning ONNX runtime into $ASSETS_DIR/ort"
pip download --quiet --only-binary :all: onnxruntime -d /tmp/ort-wheel
python - "$ASSETS_DIR/ort" <<'EOF'
import glob, sys, zipfile, os, shutil
dest = sys.argv[1]
whl = glob.glob('/tmp/ort-wheel/onnxruntime-*.whl')[0]
with zipfile.ZipFile(whl) as z:
    for name in z.namelist():
        if name.endswith(('.so', '.so.1', 'libonnxruntime.so')) or '.so.' in name:
            target = os.path.join(dest, os.path.basename(name))
            with z.open(name) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)
            print('extracted', target)
EOF

echo
echo "Assets ready in $ASSETS_DIR. The headroom service mounts this"
echo "read-only at /assets with HF_HUB_OFFLINE=1, ORT_STRATEGY=system,"
echo "ORT_LIB_LOCATION=/assets/ort, HEADROOM_UPDATE_CHECK=off."
echo "Verify (isolation proof): make headroom && the proxy compresses a"
echo "request while attached only to swarm-internal (no egress)."
