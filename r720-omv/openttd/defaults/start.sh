#!/bin/sh
# OpenTTD dedicated server launcher.
# Applies the git-managed openttd.cfg, injects server name/passwords from the
# environment, then loads the newest autosave (or generates a new game from
# openttd.cfg when there is none). POSIX sh: the image has no bash guarantee.
set -eu

cfg_dir=/config/openttd
save_dir=/data/openttd/save/autosave

# set_net_key FILE KEY VALUE: replace KEY under [network] in an OpenTTD ini,
# creating the file/section if needed. awk + ENVIRON keeps any characters in
# VALUE (passwords) literal.
set_net_key() {
  [ -f "$1" ] || printf '[network]\n' > "$1"
  grep -q '^\[network\]$' "$1" || printf '[network]\n' >> "$1"
  K="$2" V="$3" awk '
    BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"] }
    index($0, k " =") == 1 { next }
    { print }
    $0 == "[network]" { print k " = " v }
  ' "$1" > "$1.tmp"
  mv "$1.tmp" "$1"
}

[ -r "$save_dir" ] || mkdir -p "$save_dir" || {
  echo "$save_dir not usable, refusing to start a new game" >&2
  exit 1
}

cp /defaults/openttd.cfg "$cfg_dir/openttd.cfg"
set_net_key "$cfg_dir/private.cfg" server_name "${OPENTTD_SERVER_NAME:-OpenTTD}"
set_net_key "$cfg_dir/secrets.cfg" server_password "${OPENTTD_SERVER_PASSWORD:-}"
set_net_key "$cfg_dir/secrets.cfg" rcon_password "${OPENTTD_RCON_PASSWORD:-}"
set_net_key "$cfg_dir/secrets.cfg" admin_password "${OPENTTD_ADMIN_PASSWORD:-}"

save=$(ls -t "$save_dir"/autosave*.sav "$save_dir"/exit.sav 2>/dev/null | head -n1 || true)
if [ -n "$save" ]; then
  echo "Loading $save"
  exec /usr/share/games/openttd/openttd -D -d net=1 -g "$save"
fi
echo "No autosave found, generating a new game from openttd.cfg"
exec /usr/share/games/openttd/openttd -D -d net=1
