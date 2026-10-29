#!/bin/sh
# Design for a future approved server operation. Never called by local smoke.
set -eu
if [ "$#" -lt 5 ] || [ "$1" != "--approved-server-operation" ]; then
  echo "Usage: certificates.sh --approved-server-operation CONTEXT RELEASE_ENV RUNTIME_ENV bootstrap|issue|renew [CONTACT_EMAIL]" >&2
  exit 2
fi
context=$2
release=$3
runtime=$4
action=$5
contact=${6:-}
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 "$root/deploy/validate-release.py" "$release" "$runtime"
compose() {
  docker --context "$context" compose --project-name next-stop-siren \
    --env-file "$runtime" --env-file "$release" \
    -f "$root/deploy/compose.yaml" -f "$root/deploy/compose.acme.yaml" "$@"
}
case "$action" in
  bootstrap)
    docker --context "$context" compose --project-name next-stop-siren \
      --env-file "$runtime" --env-file "$release" -f "$root/deploy/compose.yaml" \
      -f "$root/deploy/compose.acme-bootstrap.yaml" up -d --no-deps nginx ;;
  issue)
    test -n "$contact"
    compose run --rm --no-deps certbot certonly --webroot -w /var/www/acme \
      --non-interactive --agree-tos --email "$contact" -d next-stop-siren.duckdns.org ;;
  renew)
    compose run --rm --no-deps certbot renew --webroot -w /var/www/acme --non-interactive
    compose exec -T nginx nginx -t
    compose exec -T nginx nginx -s reload ;;
  *) echo "Unknown certificate operation" >&2; exit 2 ;;
esac
