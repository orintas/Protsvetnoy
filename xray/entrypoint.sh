#!/bin/sh
set -eu

sed \
  -e "s|__VLESS_UUID__|${VLESS_UUID}|g" \
  -e "s|__VLESS_HOST__|${VLESS_HOST}|g" \
  -e "s|__VLESS_PORT__|${VLESS_PORT}|g" \
  -e "s|__VLESS_SNI__|${VLESS_SNI}|g" \
  -e "s|__VLESS_PBK__|${VLESS_PBK}|g" \
  -e "s|__VLESS_SID__|${VLESS_SID}|g" \
  /etc/xray/config.template.json > /etc/xray/config.json

exec xray run -c /etc/xray/config.json
