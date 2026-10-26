# One-shot init image: a shell plus `mc`. The Chainguard mc image ships no
# shell (so init.sh cannot run in it) and the official minio/mc image is not
# pullable anonymously. mc is a static Go binary, so it is copied across.
FROM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
COPY --from=cgr.dev/chainguard/minio-client@sha256:59f1924e417b6fcfdc0aca321e3bdebe8a56fd0efa7da80037d5eb65a203d687 /usr/bin/mc /usr/local/bin/mc
COPY infra/minio/init.sh /init.sh
ENTRYPOINT ["/bin/sh", "/init.sh"]
