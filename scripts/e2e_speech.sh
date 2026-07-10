#!/usr/bin/env bash
# Real-speech end-to-end test. Synthesizes speech with gTTS (needs network),
# muxes it over a test pattern, runs blasphemy-killer, and verifies the mutes.
# Gated: run with  BK_E2E=1 scripts/e2e_speech.sh
set -euo pipefail

if [[ "${BK_E2E:-}" != "1" ]]; then
    echo "skipped (set BK_E2E=1 to run)"
    exit 0
fi

cd "$(dirname "$0")/.."
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "== synthesizing speech"
uvx --from gtts gtts-cli \
    "Hello there my friend. Oh my god, that is a goddamn shame. I really like this weather. Jesus Christ, what a mess. Have a wonderful Christmas holiday." \
    -o "$work/speech.mp3"

dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$work/speech.mp3")
ffmpeg -y -nostdin -v error \
    -f lavfi -i "testsrc2=duration=${dur}:size=320x240:rate=25" \
    -i "$work/speech.mp3" \
    -map 0:v -map 1:a -c:v libx264 -preset ultrafast -c:a aac -b:a 128k -shortest \
    "$work/speech_video.mp4"

echo "== dry run"
uv run blasphemy-killer --dry-run "$work/speech_video.mp4"

echo "== real run"
uv run blasphemy-killer "$work/speech_video.mp4"

echo "== verifying"
matches=$(python3 -c "import json,sys; r=json.load(open('$work/speech_video.mp4.bk.json')); print(len(r['matches']))")
[[ "$matches" -ge 3 ]] || { echo "FAIL: expected >=3 matches, got $matches"; exit 1; }

# every muted interval must be silent
python3 - "$work/speech_video.mp4" <<'EOF'
import json, subprocess, sys
path = sys.argv[1]
report = json.load(open(path + ".bk.json"))
for start, end in report["muted_intervals"]:
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-ss", str(start + 0.05), "-to", str(end - 0.05),
         "-i", path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    line = next(l for l in proc.stderr.splitlines() if "mean_volume" in l)
    db = float(line.split("mean_volume:")[1].split()[0])
    assert db < -80, f"interval {start}-{end} not silent: {db} dB"
    print(f"  [{start:.2f}-{end:.2f}] silent ({db} dB)")
EOF

echo "== rerun should skip"
uv run blasphemy-killer "$work/speech_video.mp4" | grep -q "skipped" && echo "  marker skip OK"

echo "E2E PASSED"
