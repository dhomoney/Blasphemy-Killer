#!/usr/bin/env bash
# Containerized end-to-end test. Synthesizes speech with gTTS (needs network),
# muxes it over a test pattern, runs the image against a bind-mounted media
# directory, and verifies the mutes, the file ownership and the done-marker.
#
# Everything except gTTS runs through the image, so the host needs only docker
# and uvx -- no ffmpeg, no python env.
#
# Gated: run with  BK_E2E=1 scripts/e2e_docker.sh
set -euo pipefail

if [[ "${BK_E2E:-}" != "1" ]]; then
    echo "skipped (set BK_E2E=1 to run)"
    exit 0
fi

IMAGE=${BK_IMAGE:-blasphemy-killer:2.0.0}
MODEL=${BK_MODEL:-small}
CACHE_VOLUME=${BK_CACHE_VOLUME:-bk-e2e-cache}   # persists so the model downloads once

# Docker Desktop only bind-mounts host paths it has been given access to, and
# /tmp usually is not one of them -- so stage the work dir inside the repo,
# which the daemon can already see. BK_WORK_BASE overrides.
cd "$(dirname "$0")/.."
work=$(mktemp -d "${BK_WORK_BASE:-$PWD}/.bk-e2e.XXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/media" "$work/config" "$work/config2"

docker volume create "$CACHE_VOLUME" >/dev/null

# Run the CLI in the image. $1 is the host config dir; the rest are CLI args.
bk() {
    local config=$1; shift
    docker run --rm \
        -e PUID="$(id -u)" -e PGID="$(id -g)" \
        -v "$work/media:/media" \
        -v "$config:/config" \
        -v "$CACHE_VOLUME:/cache" \
        "$IMAGE" -m "$MODEL" "$@"
}

# ffmpeg/ffprobe from the image, so the host needs neither.
ff() {
    local tool=$1; shift
    docker run --rm --entrypoint "$tool" -v "$work:/w" -w /w "$IMAGE" "$@"
}

echo "== synthesizing speech"
uvx --from gtts gtts-cli \
    "Hello there my friend. Oh my god, that is a goddamn shame. I really like this weather. Jesus Christ, what a mess. Have a wonderful Christmas holiday." \
    -o "$work/speech.mp3"

dur=$(ff ffprobe -v error -show_entries format=duration -of csv=p=0 speech.mp3 | tr -d '\r')
ff ffmpeg -y -nostdin -v error \
    -f lavfi -i "testsrc2=duration=${dur}:size=320x240:rate=25" \
    -i speech.mp3 \
    -map 0:v -map 1:a -c:v libx264 -preset ultrafast -c:a aac -b:a 128k -shortest \
    media/speech_video.mp4

echo "== dry run"
before=$(ff ffprobe -v error -show_entries format=size -of csv=p=0 media/speech_video.mp4)
bk "$work/config" --dry-run /media/speech_video.mp4
after=$(ff ffprobe -v error -show_entries format=size -of csv=p=0 media/speech_video.mp4)
[[ "$before" == "$after" ]] || { echo "FAIL: --dry-run modified the file"; exit 1; }
echo "  file untouched by --dry-run"

echo "== real run"
bk "$work/config" /media/speech_video.mp4

echo "== verifying matches"
matches=$(python3 -c "import json;print(len(json.load(open('$work/media/speech_video.mp4.bk.json'))['matches']))")
[[ "$matches" -ge 3 ]] || { echo "FAIL: expected >=3 matches, got $matches"; exit 1; }
echo "  $matches matches"

echo "== verifying every muted interval is silent"
python3 - "$work" <<'EOF'
import json, subprocess, sys
work = sys.argv[1]
report = json.load(open(f"{work}/media/speech_video.mp4.bk.json"))
for start, end in report["muted_intervals"]:
    proc = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "ffmpeg", "-v", f"{work}:/w", "-w", "/w",
         subprocess.os.environ.get("BK_IMAGE", "blasphemy-killer:2.0.0"),
         "-nostdin", "-ss", str(start + 0.05), "-to", str(end - 0.05),
         "-i", "media/speech_video.mp4", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    line = next(l for l in proc.stderr.splitlines() if "mean_volume" in l)
    db = float(line.split("mean_volume:")[1].split()[0])
    assert db < -80, f"interval {start}-{end} not silent: {db} dB"
    print(f"  [{start:.2f}-{end:.2f}] silent ({db} dB)")
EOF

echo "== verifying host file ownership"
owner=$(stat -c '%u:%g' "$work/media/speech_video.mp4")
[[ "$owner" == "$(id -u):$(id -g)" ]] \
    || { echo "FAIL: cleaned file owned by $owner, expected $(id -u):$(id -g)"; exit 1; }
echo "  owned by $owner (not root)"

# Capture before grepping: piping docker straight into `grep -q` makes grep
# exit first, SIGPIPE the docker client, and fail the pipeline under pipefail.
echo "== rerun with the same /config should skip"
out=$(bk "$work/config" /media/speech_video.mp4)
grep -q "skipped" <<<"$out" \
    || { echo "FAIL: marker not honored across runs"; echo "$out"; exit 1; }
echo "  marker skip OK"

echo "== rerun with a FRESH /config must reprocess (the marker.key trap)"
out=$(bk "$work/config2" --dry-run /media/speech_video.mp4)
grep -q "not signed by this machine" <<<"$out" \
    || { echo "FAIL: expected an unsigned-marker reprocess with a fresh config volume"; echo "$out"; exit 1; }
echo "  fresh config volume reprocesses, as documented"

echo "DOCKER E2E PASSED"
