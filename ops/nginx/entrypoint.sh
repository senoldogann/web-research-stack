#!/bin/sh
# Generate a self-signed TLS certificate if not already present.
# The certificate is stored in the nginx-certs volume (/etc/nginx/certs).

set -e

CERT_DIR="/etc/nginx/certs"
CERT_FILE="${CERT_DIR}/server.crt"
KEY_FILE="${CERT_DIR}/server.key"
SERVER_NAME="${NGINX_SERVER_NAME:-localhost}"

mkdir -p "${CERT_DIR}"

if [ ! -f "${CERT_FILE}" ] || [ ! -f "${KEY_FILE}" ]; then
    echo "[nginx-entrypoint] Generating self-signed certificate for ${SERVER_NAME} ..."
    openssl req -x509 -nodes -days 365 \
        -newkey rsa:2048 \
        -keyout "${KEY_FILE}" \
        -out "${CERT_FILE}" \
        -subj "/CN=${SERVER_NAME}/O=WebScraper/C=US" \
        -addext "subjectAltName=DNS:${SERVER_NAME},DNS:localhost,IP:127.0.0.1"
    echo "[nginx-entrypoint] Certificate generated at ${CERT_FILE}"
else
    echo "[nginx-entrypoint] Using existing certificate at ${CERT_FILE}"
fi

# Substitute ${NGINX_SERVER_NAME} in the config template and write to a
# writable location — the mounted nginx.conf is read-only.
envsubst '${NGINX_SERVER_NAME}' \
    < /etc/nginx/nginx.conf \
    > /tmp/nginx.conf

# Hand off to nginx
exec nginx -c /tmp/nginx.conf -g "daemon off;"
