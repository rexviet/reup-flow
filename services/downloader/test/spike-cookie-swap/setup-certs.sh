#!/usr/bin/env bash
# Generates a throwaway CA + leaf certificate for the echo server, at test
# time only. Nothing here is ever committed: `.gitignore` already excludes
# `services/downloader/test/**/certs/`, and this script re-generates the
# material fresh on every run (it does not reuse a previous CA key).
#
# The leaf cert's CN/SAN is www.douyin.com (+ www.iesdouyin.com) because that
# is the hostname the real parser code dials -- see extra_hosts in
# docker-compose.spike.yml. The CA is appended into the parser container's
# certifi bundle at container start (see the `entrypoint` override in the
# compose file), which is what makes the parser's real, unmodified https
# client trust our echo server without any code change in the fork.
set -euo pipefail

cd "$(dirname "$0")"
CERT_DIR="./certs"
rm -rf "$CERT_DIR"
mkdir -p "$CERT_DIR"

# CA
openssl genrsa -out "$CERT_DIR/ca.key" 2048 >/dev/null 2>&1
openssl req -x509 -new -nodes -key "$CERT_DIR/ca.key" -sha256 -days 2 \
  -subj "/CN=spike-cookie-swap-ca" -out "$CERT_DIR/ca.pem" >/dev/null 2>&1

# Leaf, signed by the CA above, CN/SAN = the two Douyin hostnames the parser
# will be told (via extra_hosts) to resolve to the echo server's IP.
cat > "$CERT_DIR/leaf.ext" <<'EOF'
subjectAltName = DNS:www.douyin.com,DNS:www.iesdouyin.com
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
EOF

openssl genrsa -out "$CERT_DIR/leaf.key" 2048 >/dev/null 2>&1
openssl req -new -key "$CERT_DIR/leaf.key" -subj "/CN=www.douyin.com" \
  -out "$CERT_DIR/leaf.csr" >/dev/null 2>&1
openssl x509 -req -in "$CERT_DIR/leaf.csr" -CA "$CERT_DIR/ca.pem" \
  -CAkey "$CERT_DIR/ca.key" -CAcreateserial -out "$CERT_DIR/leaf.pem" \
  -days 2 -sha256 -extfile "$CERT_DIR/leaf.ext" >/dev/null 2>&1

echo "generated throwaway CA + leaf cert (CN/SAN=www.douyin.com,www.iesdouyin.com) in $CERT_DIR"
