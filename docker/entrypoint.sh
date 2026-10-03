#!/bin/sh
# Container entrypoint: ensure a TLS certificate exists, then hand off to
# uvicorn.
#
# Why TLS at all: browsers only grant microphone access in a secure context.
# On a phone that means HTTPS (or localhost). A fresh machine will not have the
# old mkcert certificate bound to 192.168.43.108, so instead of silently
# degrading to plain HTTP and breaking the mic, we generate a certificate that
# is valid for localhost plus whatever LAN IPs are declared in TLS_SAN.
#
# Certificates persist in the /certs volume, so the browser warning appears
# once per device rather than once per restart.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
CERT_DIR="${CERT_DIR:-/certs}"
PORT="${PORT:-8443}"
mkdir -p "$DATA_DIR" "$CERT_DIR"

CERT_FILE="$CERT_DIR/cert.pem"
KEY_FILE="$CERT_DIR/key.pem"

# Case 1: operator supplied their own certificate (e.g. a real CA cert).
if [ -n "${TLS_CERT_FILE:-}" ] && [ -n "${TLS_KEY_FILE:-}" ]; then
    echo "[entrypoint] using provided certificate: $TLS_CERT_FILE"
    CERT_FILE="$TLS_CERT_FILE"
    KEY_FILE="$TLS_KEY_FILE"

# Case 2: already generated on a previous boot — reuse it.
elif [ -s "$CERT_FILE" ] && [ -s "$KEY_FILE" ]; then
    echo "[entrypoint] reusing existing certificate: $CERT_FILE"

# Case 3: first boot — generate a self-signed one with the right SANs.
else
    # Modern browsers (and iOS especially) reject certificates without a
    # subjectAltName, so the SAN list is not optional.
    #
    # NOTE: we deliberately do NOT auto-detect addresses here. With bridge
    # networking the container only sees its own 172.x address, which no phone
    # can reach — phones connect to THIS MACHINE's LAN IP, so it has to come
    # from TLS_SAN. On Linux you can skip TLS_SAN by running the container with
    # `network_mode: host` (see docker-compose.yml).
    SAN_LIST="DNS:localhost,IP:127.0.0.1"

    if [ -n "${TLS_SAN:-}" ]; then
        OLD_IFS="$IFS"
        IFS=','
        for entry in $TLS_SAN; do
            entry=$(echo "$entry" | tr -d '[:space:]')
            [ -z "$entry" ] && continue
            case "$entry" in
                *:*) SAN_LIST="$SAN_LIST,$entry" ;;             # already IP: or DNS:
                *.*.*.*) SAN_LIST="$SAN_LIST,IP:$entry" ;;       # bare IPv4
                *) SAN_LIST="$SAN_LIST,DNS:$entry" ;;            # hostname
            esac
        done
        IFS="$OLD_IFS"
    else
        echo "[entrypoint] ############################################"
        echo "[entrypoint] #  WARNING: TLS_SAN is not set.               #"
        echo "[entrypoint] #  A phone opening https://<this-machine-ip>  #"
        echo "[entrypoint] #  will see a certificate name mismatch.     #"
        echo "[entrypoint] #  Fix: put this machine's LAN IP in .env:    #"
        echo "[entrypoint] #      TLS_SAN=<ip>   (find it: ip -4 -o addr  #"
        echo "[entrypoint] #      show scope global)                     #"
        echo "[entrypoint] ############################################"
    fi

    openssl req -x509 -newkey rsa:2048 -nodes \
        -days 825 \
        -keyout "$KEY_FILE" \
        -out "$CERT_FILE" \
        -subj "/CN=medi-live/O=Medi Live" \
        -addext "subjectAltName=$SAN_LIST" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
        >/dev/null 2>&1

    # Handy for the "install this profile" flow on iOS.
    cp "$CERT_FILE" "$CERT_DIR/medi-live-ca.pem"
    echo "[entrypoint] certificate written to $CERT_FILE"
fi

echo "[entrypoint] starting uvicorn on 0.0.0.0:$PORT (data: $DATA_DIR)"
cd /app
# --app-dir /app/server puts that directory on sys.path, which is what lets
# server.py's flat `from recorded import ...` resolve (it is not a package).
# exec: replace this shell so uvicorn becomes PID 1 and gets SIGTERM directly.
exec uvicorn server:app \
    --app-dir /app/server \
    --host 0.0.0.0 \
    --port "$PORT" \
    --ssl-keyfile "$KEY_FILE" \
    --ssl-certfile "$CERT_FILE" \
    --proxy-headers \
    --forwarded-allow-ips='*' \
    --workers 1