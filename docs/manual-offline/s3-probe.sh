#!/bin/sh
set -eu
call() { aws --endpoint-url "$ENDPOINT" --region us-east-1 s3api "$@"; }
key="offline-acceptance/$POD_UID"
expected='Dify offline S3 acceptance'
printf '%s' "$expected" > /tmp/upload
trap 'call delete-object --bucket "$BUCKET" --key "$key" >/dev/null' EXIT
call head-bucket --bucket "$BUCKET"
call put-object --bucket "$BUCKET" --key "$key" --body /tmp/upload >/dev/null
size=$(call get-object --bucket "$BUCKET" --key "$key" /tmp/download --query ContentLength --output text)
actual=
IFS= read -r actual < /tmp/download || true
[ "$actual" = "$expected" ]
[ "$size" -eq "${#expected}" ]
call delete-object --bucket "$BUCKET" --key "$key"
trap - EXIT
