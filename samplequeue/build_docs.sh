#!/usr/bin/env bash
# Rebuild the browser-only copy in docs/ from the app source.
# Run after changing anything in app/, seed_demo.py or sources.json, then
# commit and push; GitHub Pages serves docs/ as-is.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p docs/py/app
for f in __init__ auth db main queues rules sources synth; do
  cp "app/$f.py" "docs/py/app/$f.py"
done
cp seed_demo.py docs/py/seed_demo.py
cp app/static/app.js app/static/style.css docs/

# The in-browser demo generates fewer samples so first load is quick.
python3 - <<'PY'
import json
cfg = json.load(open("sources.json"))
for s in cfg["sources"]:
    if s["type"] == "synthetic":
        s["options"]["count"] = 3000
cfg["sources"] = [s for s in cfg["sources"] if s["type"] == "synthetic"]
json.dump(cfg, open("docs/py/sources.json", "w"), indent=2)
PY
echo "docs/ rebuilt"
