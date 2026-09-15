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

IMAGE=${BK_IMAGE:-blasphemy-killer:2.3.0}
MODEL=${BK_MODEL:-small}
CACHE_VOLUME=${BK_CACHE_VOLUME:-bk-e2e-cache}   # persists so the model downloads once

# Docker Desktop only bind-mounts host paths it has been given access to, and
# /tmp usually is not one of them -- so stage the work dir inside the repo,
# which the daemon can already see. BK_WORK_BASE overrides.
cd "$(dirname "$0")/.."
work=$(mktemp -d "${BK_WORK_BASE:-$PWD}/.bk-e2e.XXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/media" "$work/config" "$work/config2" "$work/config3"

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

# Pristine copies for the phases further down: the CLI phase below mutes
# speech_video.mp4 in place, and a cleaned file yields zero matches. The second
# one is served over HTTP rather than placed in the library -- the download
# phase has to fetch it the way a pasted URL would.
cp "$work/media/speech_video.mp4" "$work/media/web_clip.mp4"
mkdir -p "$work/serve"
cp "$work/media/speech_video.mp4" "$work/serve/sermon.mp4"

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
         subprocess.os.environ.get("BK_IMAGE", "blasphemy-killer:2.3.0"),
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

# --- web UI ----------------------------------------------------------------
# Same image, same volumes; `serve` is the only difference. Driven over HTTP
# rather than a browser so this stays runnable in CI.

echo "== web UI: starting container"
port=${BK_WEB_TEST_PORT:-18080}
name="bk-e2e-web-$$"
files="bk-e2e-files-$$"
net="bk-e2e-net-$$"
docker rm -f "$name" "$files" >/dev/null 2>&1 || true
docker network rm "$net" >/dev/null 2>&1 || true
# A user-defined network, so the download phase below can reach the file server
# by name. The published port still only listens on the host's loopback.
docker network create "$net" >/dev/null
cleanup() {
    docker rm -f "$name" "$files" >/dev/null 2>&1 || true
    docker network rm "$net" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
docker run -d --name "$name" --network "$net" \
    -e PUID="$(id -u)" -e PGID="$(id -g)" \
    -p "127.0.0.1:${port}:8080" \
    -v "$work/media:/media" \
    -v "$work/config3:/config" \
    -v "$CACHE_VOLUME:/cache" \
    "$IMAGE" serve >/dev/null

for _ in $(seq 1 30); do
    curl -sf --max-time 2 "http://127.0.0.1:${port}/api/config" >/dev/null 2>&1 && break
    sleep 1
done

echo "== web UI: assets load (no CDN, so this must work offline too)"
for path in / /static/app.js /static/style.css; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}${path}")
    [[ "$code" == "200" ]] || { echo "FAIL: $path returned $code"; exit 1; }
    echo "  $path -> 200"
done

echo "== web UI: refuses to escape the media root"
for bad in '../../etc' '/etc' '..'; do
    code=$(curl -s -o /dev/null -w '%{http_code}' \
        "http://127.0.0.1:${port}/api/browse?path=$(printf '%s' "$bad" | jq -sRr @uri 2>/dev/null || echo "$bad")")
    [[ "$code" == "400" ]] || { echo "FAIL: traversal '$bad' returned $code, expected 400"; exit 1; }
done
echo "  traversal attempts rejected"

echo "== web UI: browse lists the media directory"
curl -s "http://127.0.0.1:${port}/api/browse" \
    | grep -q 'speech_video.mp4' \
    || { echo "FAIL: browse did not list the fixture"; exit 1; }
echo "  fixture listed"

echo "== web UI: runs a job end to end"
job=$(curl -s -X POST "http://127.0.0.1:${port}/api/jobs" \
        -H 'Content-Type: application/json' \
        -d '{"path":"web_clip.mp4","dry_run":false,"force":true}' \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['job']['id'])")
for _ in $(seq 1 120); do
    state=$(curl -s "http://127.0.0.1:${port}/api/jobs" | python3 -c "
import json,sys
j=[x for x in json.load(sys.stdin)['jobs'] if x['id']=='$job'][0]
print(j['state'])")
    [[ "$state" == "done" || "$state" == "failed" ]] && break
    sleep 1
done
[[ "$state" == "done" ]] || { echo "FAIL: web job ended as $state"; exit 1; }

curl -s "http://127.0.0.1:${port}/api/jobs" | python3 -c "
import json,sys
j=[x for x in json.load(sys.stdin)['jobs'] if x['id']=='$job'][0]
assert len(j['matches']) >= 3, f\"expected >=3 matches, got {len(j['matches'])}\"
assert j['muted'] >= 1, 'nothing was muted'
assert j['progress'] == 1.0, f\"progress stalled at {j['progress']}\"
print(f\"  {len(j['matches'])} matches, {j['muted']} interval(s) muted\")"

owner=$(stat -c '%u:%g' "$work/media/web_clip.mp4")
[[ "$owner" == "$(id -u):$(id -g)" ]] \
    || { echo "FAIL: web-cleaned file owned by $owner"; exit 1; }
echo "  cleaned file owned by $owner (not root)"

# --- download, then the clean it promises ----------------------------------
# The whole point of pasting a URL is to end up with a cleaned file, so the
# release gate has to see both halves happen off one request. Served from a
# sidecar on the private network rather than the internet: this has to pass
# offline, and it must not depend on some video still existing on YouTube.

echo "== download: starting the file server"
docker run -d --name "$files" --network "$net" \
    -v "$work/serve:/srv:ro" --entrypoint python \
    "$IMAGE" -m http.server 8000 --directory /srv >/dev/null

for _ in $(seq 1 30); do
    docker run --rm --network "$net" --entrypoint python "$IMAGE" -c "
import sys, urllib.request
try:
    urllib.request.urlopen('http://${files}:8000/sermon.mp4', timeout=2).read(1)
except Exception:
    sys.exit(1)" >/dev/null 2>&1 && break
    sleep 1
done

echo "== download: a pasted URL cleans itself"
# No "clean" in the body: the default is what is under test.
dl=$(curl -s -X POST "http://127.0.0.1:${port}/api/downloads" \
        -H 'Content-Type: application/json' \
        -d "{\"url\":\"http://${files}:8000/sermon.mp4\"}" \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['job']['id'])")

# The clean is a separate job, created by the worker once the download names
# its file, and found by the download id it carries.
for _ in $(seq 1 180); do
    read -r state landed <<<"$(curl -s "http://127.0.0.1:${port}/api/jobs" | python3 -c "
import json,sys
jobs=json.load(sys.stdin)['jobs']
follow=[j for j in jobs if j.get('source') == '$dl']
print(follow[0]['state'], follow[0]['path']) if follow else print('pending', '')")"
    [[ "$state" == "done" || "$state" == "failed" || "$state" == "cancelled" ]] && break
    sleep 1
done
[[ "$state" == "done" ]] || { echo "FAIL: the promised clean ended as $state"; exit 1; }

curl -s "http://127.0.0.1:${port}/api/jobs" | python3 -c "
import json,sys
jobs=json.load(sys.stdin)['jobs']
dl=[j for j in jobs if j['id']=='$dl'][0]
clean=[j for j in jobs if j.get('source')=='$dl'][0]
assert dl['kind'] == 'download' and dl['clean'] is True, dl
assert clean['dry_run'] is False, 'the promised clean must not be a dry run'
assert len(clean['matches']) >= 3, f\"expected >=3 matches, got {len(clean['matches'])}\"
assert clean['muted'] >= 1, 'nothing was muted'
print(f\"  downloaded and cleaned: {len(clean['matches'])} matches, {clean['muted']} interval(s) muted\")"

owner=$(stat -c '%u:%g' "$work/media/$landed")
[[ "$owner" == "$(id -u):$(id -g)" ]] \
    || { echo "FAIL: downloaded file owned by $owner"; exit 1; }
echo "  '$landed' owned by $owner (not root)"

echo "DOCKER E2E PASSED"
