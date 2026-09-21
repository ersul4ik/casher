#!/usr/bin/env bash
#
# Copy the whole database from one Postgres to another — used when moving the Neon
# project to a region next to the service. Both connection strings stay in the
# environment and never reach the process list.
#
#     SRC_DATABASE_URL='postgresql://...us-east-2.aws.neon.tech/neondb?sslmode=require' \
#     DST_DATABASE_URL='postgresql://...eu-central-1.aws.neon.tech/neondb?sslmode=require' \
#     scripts/move_db.sh
#
# pg_dump has to be at least as new as the server, and Neon keeps its Postgres current,
# so the work runs in a container whose version is picked to match rather than on
# whatever brew installed. The destination is overwritten: its tables are dropped first.

set -euo pipefail

# Any psql can ask a server its version; pg_dump is the picky one, so the image that
# carries out the move is chosen to match. PG_IMAGE pins both to an image of your own.
PROBE_IMAGE="${PG_IMAGE:-postgres:17-alpine}"

: "${SRC_DATABASE_URL:?set SRC_DATABASE_URL to the database you are copying FROM}"
: "${DST_DATABASE_URL:?set DST_DATABASE_URL to the database you are copying TO}"

if [ "$SRC_DATABASE_URL" = "$DST_DATABASE_URL" ]; then
    echo "Source and destination are the same database. Nothing to do." >&2
    exit 1
fi

host_of() { printf '%s\n' "$1" | sed -E 's|^[^@]*@||; s|[/?].*$||'; }

# Neon's console hands out the pooled string by default. pg_dump needs a session of its
# own and does not survive pgbouncer, and the direct host is the same name without
# "-pooler", so take that instead of making the operator hunt for the toggle.
unpool() { printf '%s\n' "$1" | sed -E 's|@([^/?]*)-pooler\.|@\1.|'; }

server_major() {
    docker run --rm -e PGURL="$1" "$PROBE_IMAGE" \
        sh -c 'psql "$PGURL" -At -c "SHOW server_version_num"' | cut -c1-2
}

count_rows() {
    docker run --rm -i -e PGURL="$1" "$IMAGE" sh -c 'psql "$PGURL" -At -f -' <<'SQL' 2>/dev/null || echo "no tables"
SELECT string_agg(t || '=' || n, ' ' ORDER BY t)
  FROM (
    SELECT 'users' AS t, count(*) AS n FROM users
    UNION ALL SELECT 'categories', count(*) FROM categories
    UNION ALL SELECT 'spends', count(*) FROM spends
    UNION ALL SELECT 'tags', count(*) FROM tags
    UNION ALL SELECT 'spend_tags', count(*) FROM spend_tags
  ) x
SQL
}

for name in SRC_DATABASE_URL DST_DATABASE_URL; do
    eval "value=\$$name"
    direct="$(unpool "$value")"
    if [ "$direct" != "$value" ]; then
        echo "$name points at the pooled endpoint; using $(host_of "$direct") instead."
        eval "$name=\$direct"
    fi
done

echo "From: $(host_of "$SRC_DATABASE_URL")"
echo "To:   $(host_of "$DST_DATABASE_URL")"

src_major="$(server_major "$SRC_DATABASE_URL")"
dst_major="$(server_major "$DST_DATABASE_URL")"
case "${src_major}${dst_major}" in
    "" | *[!0-9]*)
        echo "Could not read the server version — check the connection strings." >&2
        exit 1
        ;;
esac
major="$src_major"
[ "$dst_major" -gt "$major" ] && major="$dst_major"
IMAGE="${PG_IMAGE:-postgres:${major}-alpine}"
echo "Postgres $src_major -> $dst_major, moving with $IMAGE"
echo

before="$(count_rows "$SRC_DATABASE_URL")"
echo "Source holds: $before"

read -r -p "Overwrite the destination with this? [y/N] " answer
case "$answer" in
    y | Y) ;;
    *)
        echo "Cancelled."
        exit 1
        ;;
esac

docker run --rm -e SRC="$SRC_DATABASE_URL" -e DST="$DST_DATABASE_URL" "$IMAGE" \
    sh -c 'pg_dump -Fc --no-owner --no-acl "$SRC" | pg_restore -d "$DST" --no-owner --no-acl --clean --if-exists'

after="$(count_rows "$DST_DATABASE_URL")"
echo
echo "Destination holds: $after"

if [ "$before" = "$after" ]; then
    echo "Row counts match. Point DATABASE_URL at the new database and redeploy."
else
    echo "Row counts DIFFER — do not switch over until you know why." >&2
    exit 1
fi
