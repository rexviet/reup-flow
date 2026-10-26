#!/bin/sh
# Idempotent. Creates buckets, one MinIO user per service with a prefix-scoped
# policy (plan §6), and the lifecycle rule for work/.
set -eu

: "${MINIO_ENDPOINT:?}" "${MINIO_ROOT_USER:?}" "${MINIO_ROOT_PASSWORD:?}"
: "${S3_ACCESS_KEY_ID:?}" "${S3_SECRET_ACCESS_KEY:?}"
: "${LOC_S3_ACCESS_KEY_ID:?}" "${LOC_S3_SECRET_ACCESS_KEY:?}"
: "${RENDER_S3_ACCESS_KEY_ID:?}" "${RENDER_S3_SECRET_ACCESS_KEY:?}"
: "${LANGFUSE_S3_ACCESS_KEY_ID:?}" "${LANGFUSE_S3_SECRET_ACCESS_KEY:?}"

if [ "$MINIO_ROOT_USER" = "minioadmin" ] || [ "$MINIO_ROOT_PASSWORD" = "minioadmin" ]; then
  echo "minio-init: refusing to run with the default minioadmin root credential" >&2
  exit 1
fi

MEDIA=reup-media
LF=langfuse
POLICY_DIR=/tmp/policies
mkdir -p "$POLICY_DIR"

# MinIO may report healthy a moment before admin APIs answer.
i=0
until mc alias set local "$MINIO_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 30 ] || { echo "minio-init: minio unreachable" >&2; exit 1; }
  sleep 1
done

mc mb --ignore-existing "local/$MEDIA" "local/$LF" >/dev/null

# $1 policy name, $2 bucket, $3 "Resource-suffix:actions" ... written by callers
# via the helpers below. Statement building is explicit JSON on purpose: a
# policy is a security boundary and should read as one.
write_policy() { cat > "$POLICY_DIR/$1.json"; }

BUCKET_ARN="arn:aws:s3:::$MEDIA"

# downloader: only source/*
write_policy downloader <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["$BUCKET_ARN/source/*"]},
 {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":["$BUCKET_ARN"],"Condition":{"StringLike":{"s3:prefix":["source/*","source/"]}}},
 {"Effect":"Allow","Action":["s3:ListBucketMultipartUploads"],"Resource":["$BUCKET_ARN"]}
]}
JSON

# localization: read source/*, read-write work/ cache/ review/, write-only output/
write_policy localization <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject"],"Resource":["$BUCKET_ARN/source/*"]},
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["$BUCKET_ARN/work/*","$BUCKET_ARN/cache/*","$BUCKET_ARN/review/*"]},
 {"Effect":"Allow","Action":["s3:PutObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["$BUCKET_ARN/output/*"]},
 {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":["$BUCKET_ARN"],"Condition":{"StringLike":{"s3:prefix":["source/*","work/*","cache/*","review/*","output/*"]}}},
 {"Effect":"Allow","Action":["s3:ListBucketMultipartUploads"],"Resource":["$BUCKET_ARN"]}
]}
JSON

# renderer: only work/*
write_policy renderer <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["$BUCKET_ARN/work/*"]},
 {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":["$BUCKET_ARN"],"Condition":{"StringLike":{"s3:prefix":["work/*"]}}}
]}
JSON

# langfuse: its own bucket, nothing in reup-media
write_policy langfuse <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["arn:aws:s3:::$LF/*"]},
 {"Effect":"Allow","Action":["s3:ListBucket","s3:ListBucketMultipartUploads","s3:GetBucketLocation"],"Resource":["arn:aws:s3:::$LF"]}
]}
JSON

# $1 user, $2 secret, $3 policy. Re-adding a user resets its secret, so a
# rotated .env value takes effect on the next run.
ensure_user() {
  mc admin policy create local "$3" "$POLICY_DIR/$3.json" >/dev/null
  mc admin user add local "$1" "$2" >/dev/null
  mc admin policy attach local "$3" --user "$1" >/dev/null 2>&1 || true
}
ensure_user "$S3_ACCESS_KEY_ID"          "$S3_SECRET_ACCESS_KEY"          downloader
ensure_user "$LOC_S3_ACCESS_KEY_ID"      "$LOC_S3_SECRET_ACCESS_KEY"      localization
ensure_user "$RENDER_S3_ACCESS_KEY_ID"   "$RENDER_S3_SECRET_ACCESS_KEY"   renderer
ensure_user "$LANGFUSE_S3_ACCESS_KEY_ID" "$LANGFUSE_S3_SECRET_ACCESS_KEY" langfuse

# work/ expires after 30 days (a workflow may outlive a week while backfill
# queues behind one GPU). cache/, review/ and output/ get NO lifecycle: the TTS
# cache and the review evidence must outlive the workflow that wrote them.
mc ilm rule remove --all --force "local/$MEDIA" >/dev/null 2>&1 || true
mc ilm rule add --prefix "work/" --expire-days 30 "local/$MEDIA" >/dev/null

echo "minio-init: ok"
