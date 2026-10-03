#!/bin/sh
# Build an offline release bundle: the Docker image plus everything needed to
# run it on a machine that cannot (or should not) build it.
#
#   ./docker/bundle.sh                 # -> dist/medi-live-<version>-<date>/
#   RAW=1 ./docker/bundle.sh           # uncompressed .tar (faster to load)
#   ZSTD=1 ./docker/bundle.sh          # .tar.zst (smallest, needs zstd)
#
# Copy the resulting folder to a USB stick. On the target machine:
#   gunzip -c medi-live-image.tar.gz | docker load
#   cp .env.example .env      # set ADMIN_TOKEN + TLS_SAN
#   docker compose up -d
set -eu

IMAGE="${IMAGE:-medi-live:latest}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

VERSION="${VERSION:-$(git describe --tags --always 2>/dev/null || echo dev)}"
DATE="$(date +%Y%m%d)"
ARCH="$(uname -m)"
BUNDLE="dist/medi-live-${VERSION}-${DATE}-${ARCH}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "[bundle] ERROR: image '$IMAGE' not found. Build it first:" >&2
    echo "         docker compose build" >&2
    exit 1
fi

echo "[bundle] version  : $VERSION"
echo "[bundle] image    : $IMAGE"
echo "[bundle] contents : $BUNDLE"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/server/data" "$BUNDLE/certs"

# ---- prebuilt-image compose + config template (no build step on target) ----
cp docker-compose.deploy.yml "$BUNDLE/docker-compose.yml"
cp .env.docker.example       "$BUNDLE/.env.example"
touch "$BUNDLE/server/data/.gitkeep" "$BUNDLE/certs/.gitkeep"

# ---- the image itself ----
# docker save writes an uncompressed tar of the layers. Compression is applied
# afterwards so the result can be piped straight into `docker load`.
echo "[bundle] saving image (this takes a few minutes for ~2GB)..."
docker save "$IMAGE" -o "$BUNDLE/medi-live-image.tar"

SIZE_BEFORE="$(du -h "$BUNDLE/medi-live-image.tar" | cut -f1)"
if [ "${ZSTD:-0}" = "1" ]; then
    echo "[bundle] compressing with zstd..."
    zstd -q -T0 -3 "$BUNDLE/medi-live-image.tar" -o "$BUNDLE/medi-live-image.tar.zst"
    rm -f "$BUNDLE/medi-live-image.tar"
    IMG_FILE="medi-live-image.tar.zst"
    LOAD_HINT="zstd -dc medi-live-image.tar.zst | docker load"
elif [ "${RAW:-0}" = "1" ]; then
    IMG_FILE="medi-live-image.tar"
    LOAD_HINT="docker load -i medi-live-image.tar"
else
    echo "[bundle] compressing with gzip..."
    gzip -1 "$BUNDLE/medi-live-image.tar"
    IMG_FILE="medi-live-image.tar.gz"
    LOAD_HINT="gunzip -c medi-live-image.tar.gz | docker load"
fi

IMAGE_SHA="$(sha256sum "$BUNDLE/$IMG_FILE" | cut -d' ' -f1)"
TOTAL="$(du -sh "$BUNDLE" | cut -f1)"

# ---- install instructions, written next to the files they describe ----
cat > "$BUNDLE/INSTALL.txt" <<EOF
Medi Live $VERSION — offline bundle
================================================================

WHAT THIS IS
  Prebuilt Docker image + a compose file. The target machine does NOT
  compile anything, and does NOT need internet access, Python or Node.
  Docker is the only requirement.

INSTALL (about 2 minutes after copying this folder across)
  1. Load the image (~1 minute, writes ~2GB to your Docker storage):
       $LOAD_HINT

  2. Create your settings:
       cp .env.example .env

  3. Edit .env — two values matter:
       ADMIN_TOKEN = any private string  (admin panel login)
       TLS_SAN     = THIS machine's LAN IP, so your phone can connect.
                     Find it with:  ip -4 -o addr show scope global

  4. Start:
       docker compose up -d
       docker compose logs -f      # wait for "Application startup complete"

  5. Open https://<this-machine-ip>:8443 on the phone and accept the
     certificate warning once per device (required for microphone access).

VERIFY
  curl -k https://localhost:8443/health      # expect "ok": true
  docker compose ps                          # expect: healthy

DAILY USE
  docker compose logs -f        watch
  docker compose restart        restart
  docker compose down           stop (your data survives)
  docker compose up -d          start again

BACKUP / MOVE TO ANOTHER MACHINE
  Copy these two folders — they hold everything (recordings, per-class
  vocabularies, rooms, certificates):
      server/data/
      certs/

TROUBLESHOOTING
  Microphone blocked, no error
      TLS_SAN is wrong. Set the IP from step 3, then re-run:
          docker compose up -d --force-recreate
  Port already in use
      Change PORT in .env (e.g. 9443), then: docker compose up -d
  "no such image"
      Step 1 did not finish. Re-run the load command and check for errors.
  Want to reset everything
      docker compose down && rm -rf server/data certs

IMAGE DETAILS
  file      : $IMG_FILE
  raw size  : $SIZE_BEFORE
  bundle    : $TOTAL
  sha256    : $IMAGE_SHA
  image tag : $IMAGE
  platform  : linux/$ARCH

  Verify the copy arrived intact (compare with the value above):
      sha256sum $IMG_FILE
EOF

echo "[bundle] done: $TOTAL total"
echo "[bundle] image sha256: $IMAGE_SHA"
echo "[bundle] copy this folder to the target machine: $BUNDLE"