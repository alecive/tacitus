#!/usr/bin/env bash
# zulip_switch.sh: the owner's side of moving the EA from Slack to Zulip.
#
# Run as the owner from a login shell (docker group). Every step says what it is
# about to do and asks first; every step can be re-run. Secrets are never
# printed.
#
#   scripts/zulip_switch.sh            all steps, in order
#   scripts/zulip_switch.sh --status   where things stand, changes nothing
#   scripts/zulip_switch.sh --rollback the bot back on Slack (Zulip stays up)
#
# Steps: 1 tailnet HTTPS for the server, 2 point the realm at that address,
# 3 owner password, 4 the bot's site setting, 5 realm check, 6 room carry-over,
# 7 the switch itself (checked with a real 'usage' as you), 8 your devices.
# The live smoke test (tests/integration) is for a server the bot is NOT on:
# it runs its own bot instance, and two instances answer the same traffic.
#
# The chat block has two copies. The owner's processes (the bot) read the
# owner-private chat.json; the resident reads ea-config.json for wording.
# Every change here writes both. The bot's API key is read and stored by the
# owner_trust rule: the owner-private secrets.env, then the legacy
# .env.secrets only if it passes the ownership checks.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE="$REPO/deploy/zulip/docker-compose.yml"
# The stack's env file: the owner-private one, else the gitignored
# fallback in the checkout (deploy/zulip/env.sh).
. "$REPO/deploy/zulip/env.sh"
ENVF="$(zulip_env_file)"
LIVE_CONF=/home/ea-bot/.ea/config/ea-config.json
SECRETS=/home/ea-bot/.ea/.env.secrets
# The owner runs this (the preflight checks): the account that owns the
# checkout, unless EA_OWNER names it. Its private dir is in its passwd home.
OWNER="${EA_OWNER:-$(stat -c %U "$REPO")}"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
PRIV="${EA_PRIVATE_DIR:-$OWNER_HOME/.ea-private}"
OWNER_SECRETS="$PRIV/secrets.env"
OWNER_CHAT="$PRIV/chat.json"
OWNER_TRUST="$REPO/preprocessor/owner_trust.py"
BOT_LOG=/home/ea-bot/.ea/logs/slack-bot.log
LOCAL_HTTP=http://127.0.0.1:8080
HTTPS_PORT=8443
OWNER_EMAIL="${ZULIP_ADMIN_EMAIL:-$(grep -E "^ZULIP_ADMIN_EMAIL=" "$ENVF" 2>/dev/null | cut -d= -f2-)}"
PY="$REPO/.venv/bin/python3"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   ok    %s\n' "$*"; }
warn() { printf '   warn  %s\n' "$*"; }
die()  { printf '   FAIL  %s\n' "$*" >&2; exit 1; }
ask()  { local a; read -r -p "   $1 [y/N] " a; [[ "$a" == [yY]* ]]; }
dkr()  { if docker ps >/dev/null 2>&1; then docker "$@"; else sg docker -c "docker $*"; fi; }
compose() { dkr compose -f "$COMPOSE" --env-file "$ENVF" "$@"; }
chat_get() {  # $1 = key path (frontend, zulip.site): the owner's copy when it exists
    $PY - "$OWNER_CHAT" "$LIVE_CONF" "$1" <<'PY'
import json, os, sys
own, live, key = sys.argv[1:4]
d = json.load(open(own)) if os.path.exists(own) else json.load(open(live)).get("chat", {})
for k in key.split("."):
    d = d.get(k, "") if isinstance(d, dict) else ""
print(d if isinstance(d, str) else "")
PY
}
live_get() { chat_get "$1"; }
live_zulip_get() { chat_get "zulip.$1"; }
set_chat() {  # $1 = key path, $2 = value: written to the resident's copy and the owner's
    sudo -n -u ea-bot python3 - "$1" "$2" <<'PY'
import json, sys
p = "/home/ea-bot/.ea/config/ea-config.json"; d = json.load(open(p))
*parents, last = sys.argv[1].split("."); node = d.setdefault("chat", {})
for k in parents: node = node.setdefault(k, {})
node[last] = sys.argv[2]
open(p, "w").write(json.dumps(d, indent=2) + "\n"); print(f"   live config: chat.{sys.argv[1]} = {sys.argv[2]}")
PY
    [ -f "$OWNER_CHAT" ] || $PY "$OWNER_TRUST" seed --chat
    $PY - "$OWNER_CHAT" "$1" "$2" <<'PY'
import json, os, sys, tempfile
p, key, val = sys.argv[1:4]
d = json.load(open(p))
*parents, last = key.split("."); node = d
for k in parents: node = node.setdefault(k, {})
node[last] = val
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".chat.json.")
with os.fdopen(fd, "w") as f: f.write(json.dumps(d, indent=2) + "\n")
os.chmod(tmp, 0o600); os.replace(tmp, p); print(f"   owner copy:  chat.{key} = {val} ({p})")
PY
}
count_key() { local n; n=$(grep -sc "^$1=" "$2" || true); echo "${n:-0}"; }
tailnet_name() { tailscale status --json | $PY -c "import json,sys;print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))"; }
owner_key() {  # prints the owner's API key to stdout for capture into a variable, never to the terminal
    local pw; pw=$(grep '^ZULIP_OWNER_PASSWORD=' "$ENVF" | cut -d= -f2-)
    curl -sS -X POST "$1/api/v1/fetch_api_key" --data-urlencode "username=$OWNER_EMAIL" --data-urlencode "password=$pw" \
      | $PY -c "import json,sys;print(json.load(sys.stdin).get('api_key',''))"
}
bot_key() { $PY "$OWNER_TRUST" secret ZULIP_API_KEY; }  # prints the key for capture into a variable; exit 1 if none
zulip_healthy() { [ "$(dkr inspect --format '{{.State.Health.Status}}' ea-zulip-zulip-1 2>/dev/null)" = "healthy" ]; }
wait_healthy() { local i; for i in $(seq 1 30); do zulip_healthy && return 0; sleep 10; done; return 1; }

status() {
    say "Status"
    printf '   bot frontend: %s\n' "$(live_get frontend)"
    printf '   bot site:     %s\n' "$(live_zulip_get site)"
    printf '   zulip container: %s\n' "$(dkr inspect --format '{{.State.Health.Status}}' ea-zulip-zulip-1 2>/dev/null || echo not-running)"
    printf '   serve on :%s: %s\n' "$HTTPS_PORT" "$(tailscale serve status 2>/dev/null | grep -c ":$HTTPS_PORT" || true)"
    printf '   chat settings: %s\n' "$([ -f "$OWNER_CHAT" ] && echo "$OWNER_CHAT" || echo "$LIVE_CONF (no owner copy yet)")"
    printf '   ZULIP_API_KEY in owner secrets: %s, in legacy secrets: %s\n' \
        "$(count_key ZULIP_API_KEY "$OWNER_SECRETS")" "$(count_key ZULIP_API_KEY "$SECRETS")"
    printf '   zulip rooms in registry: %s\n' "$($PY -c "import json;d=json.load(open('/home/ea-bot/.ea/config/ea-channels.json'));print(sum(1 for k in d if k.startswith('stream:')))")"
}

rollback() {
    say "Rollback: the bot back on Slack"
    set_chat frontend slack
    sudo -n /usr/bin/systemctl restart "$(systemctl cat ea-chat-bot.service >/dev/null 2>&1 && echo ea-chat-bot || echo ea-slack-bot).service"; sleep 12
    grep -q "Channel validated" <(tail -n 40 "$BOT_LOG") && ok "bot is back on Slack" || warn "no 'Channel validated' line yet; check $BOT_LOG"
}

case "${1:-}" in
    --status) status; exit 0 ;;
    --rollback) rollback; exit 0 ;;
    "") ;;
    *) echo "usage: $0 [--status|--rollback]"; exit 2 ;;
esac

say "Preflight"
[ "$(id -un)" = "$OWNER" ] || die "run this as the owner ($OWNER)"
id -nG | grep -qw docker || die "this shell has no docker group; open a new login shell (or run: newgrp docker)"
command -v tailscale >/dev/null || die "tailscale not found"
[ -f "$ENVF" ] || die "missing $ENVF"
zulip_healthy || die "the Zulip container is not healthy; start it: docker compose -f $COMPOSE --env-file $ENVF up -d"
ok "$OWNER, docker, tailscale, Zulip container healthy"

say "Step 1: HTTPS on the tailnet"
DNS=$(tailnet_name); SITE="https://$DNS:$HTTPS_PORT"
echo "   The server will be reachable at $SITE (tailnet only; Funnel is never used)."
if tailscale serve status 2>/dev/null | grep -q ":$HTTPS_PORT"; then
    ok "serve already configured on :$HTTPS_PORT"
elif ask "Configure Tailscale Serve to proxy :$HTTPS_PORT to $LOCAL_HTTP now?"; then
    tailscale serve --bg --https="$HTTPS_PORT" "$LOCAL_HTTP" 2>/dev/null \
      || sudo tailscale serve --bg --https="$HTTPS_PORT" "$LOCAL_HTTP"
    ok "serve configured"
else
    die "the remaining steps need the tailnet address"
fi
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$SITE/" || true)
[ "$code" = 200 ] || [ "$code" = 302 ] || warn "GET $SITE/ answered $code (the realm may still point at the old host; step 2 fixes that)"

say "Step 2: point the realm at $DNS:$HTTPS_PORT"
current=$(grep -o 'SETTING_EXTERNAL_HOST: "[^"]*"' "$COMPOSE" | cut -d'"' -f2)
if [ "$current" = "$DNS:$HTTPS_PORT" ]; then
    ok "compose already says $current"
elif ask "Change SETTING_EXTERNAL_HOST from $current to $DNS:$HTTPS_PORT and recreate the container?"; then
    sed -i "s#SETTING_EXTERNAL_HOST: \"[^\"]*\"#SETTING_EXTERNAL_HOST: \"$DNS:$HTTPS_PORT\"#" "$COMPOSE"
    compose up -d >/dev/null
    wait_healthy || die "the Zulip container did not come back healthy; docker logs ea-zulip-zulip-1"
    ok "container recreated and healthy"
fi
realm=$(curl -s --max-time 20 "$SITE/api/v1/server_settings" | $PY -c "import json,sys;print(json.load(sys.stdin).get('realm_url',''))" || true)
[ "$realm" = "$SITE" ] && ok "realm_url is $realm" || warn "realm_url is '$realm', expected $SITE"

say "Step 3: your password"
if ask "Set a new password for $OWNER_EMAIL now? (the generated one stays in $ENVF until you do)"; then
    OKEY=$(owner_key "$SITE"); [ -n "$OKEY" ] || die "could not fetch the owner's API key with the current password"
    read -r -s -p "   new password: " NEWPW; echo; read -r -s -p "   again: " NEWPW2; echo
    [ "$NEWPW" = "$NEWPW2" ] || die "passwords differ"
    OLDPW=$(grep '^ZULIP_OWNER_PASSWORD=' "$ENVF" | cut -d= -f2-)
    res=$(curl -sS -u "$OWNER_EMAIL:$OKEY" -X PATCH "$SITE/api/v1/settings" --data-urlencode "old_password=$OLDPW" --data-urlencode "new_password=$NEWPW" \
      | $PY -c "import json,sys;d=json.load(sys.stdin);print(d.get('result'), d.get('msg',''))")
    if [[ "$res" == success* ]]; then
        sed -i "s#^ZULIP_OWNER_PASSWORD=.*#ZULIP_OWNER_PASSWORD=$NEWPW#" "$ENVF"; chmod 600 "$ENVF"; ok "password changed and recorded in $ENVF (mode 600)"
    else
        warn "the API refused ($res); change it in Zulip under Settings, then update ZULIP_OWNER_PASSWORD in $ENVF by hand"
    fi
    unset NEWPW NEWPW2 OLDPW OKEY
fi

say "Step 4: the bot's site setting"
if [ "$(live_zulip_get site)" = "$SITE" ]; then
    ok "live config already has site $SITE"
elif ask "Set chat.zulip.site to $SITE in the live config (both copies)?"; then
    set_chat zulip.site "$SITE"
fi
if ! bot_key >/dev/null; then
    [ -f "$OWNER_SECRETS" ] || $PY "$OWNER_TRUST" seed --secrets
    OKEY=$(owner_key "$SITE"); [ -n "$OKEY" ] || die "owner key needed to read the bot key"
    ( umask 077; $PY "$REPO/scripts/zulip_bootstrap.py" --site "$SITE" --owner-email "$OWNER_EMAIL" --owner-key "$OKEY" 2>/dev/null \
        | grep '^ZULIP_API_KEY=' >> "$OWNER_SECRETS" )
    chmod 600 "$OWNER_SECRETS"
    unset OKEY; ok "ZULIP_API_KEY added to $OWNER_SECRETS"
else
    ok "ZULIP_API_KEY found (owner secrets, or the legacy file after the ownership checks)"
fi

say "Step 5: realm check"
OKEY=$(owner_key "$SITE"); [ -n "$OKEY" ] || die "could not fetch the owner's API key"
$PY "$REPO/scripts/zulip_bootstrap.py" --check --site "$SITE" --owner-email "$OWNER_EMAIL" --owner-key "$OKEY" || die "bootstrap --check failed"

say "Step 6: carry the room specialists over"
echo "   Each Slack project room becomes a private stream registered with the SAME session id, so every specialist resumes with its history."
ZULIP_API_KEY="$(bot_key)" $PY "$REPO/scripts/zulip_migrate_rooms.py" --site "$SITE"
if ask "Create the streams and registry rows listed above (already-migrated rooms are skipped)?"; then
    ZULIP_API_KEY="$(bot_key)" $PY "$REPO/scripts/zulip_migrate_rooms.py" --site "$SITE" --apply
fi

say "Step 7: the switch"
if [ "$(live_get frontend)" = zulip ]; then
    ok "the bot is already on Zulip"
elif ask "Put the bot on Zulip now (config flip + restart; Slack stays as rollback: $0 --rollback)?"; then
    set_chat frontend zulip
    sudo -n /usr/bin/systemctl restart "$(systemctl cat ea-chat-bot.service >/dev/null 2>&1 && echo ea-chat-bot || echo ea-slack-bot).service"; sleep 15
    if tail -n 60 "$BOT_LOG" | grep -q "frontend=zulip stream=ea (private, subscribed)"; then ok "$(tail -n 60 "$BOT_LOG" | grep 'frontend=zulip' | tail -1 | cut -c21-)"; else warn "startup line not seen yet; tail $BOT_LOG"; fi
    echo "   Checking the live bot: posting 'usage' as you in a new topic of 'ea' and waiting for its reply."
    topic="hello $(date +%H:%M)"
    curl -sS -u "$OWNER_EMAIL:$OKEY" -X POST "$SITE/api/v1/messages" -d type=stream -d to=ea --data-urlencode "topic=$topic" --data-urlencode "content=usage" >/dev/null
    for i in $(seq 1 12); do
        sleep 5
        n=$(curl -sS -u "$OWNER_EMAIL:$OKEY" -G "$SITE/api/v1/messages" --data-urlencode "anchor=newest" --data-urlencode "num_before=3" --data-urlencode "num_after=0" \
            --data-urlencode "narrow=[{\"operator\":\"channel\",\"operand\":\"ea\"},{\"operator\":\"topic\",\"operand\":\"$topic\"}]" \
            | $PY -c "import json,sys;print(sum(1 for m in json.load(sys.stdin).get('messages',[]) if m['sender_email']!='$OWNER_EMAIL'))")
        [ "${n:-0}" -ge 1 ] && break
    done
    [ "${n:-0}" -ge 1 ] && ok "the bot answered in topic '$topic'" || warn "no reply within 60 s; tail $BOT_LOG"
fi
unset OKEY

say "Step 8: your devices"
cat <<TXT
   Server URL:  $SITE   (tailnet only; the device must be on your Tailscale)
   Sign in as:  $OWNER_EMAIL with the password from step 3
   Apps:        https://zulip.com/apps/   (desktop and mobile). The first login asks you to accept the terms.
   Try it:      in stream "ea" send "usage" with no topic; the EA moves it into a topic of its own and answers there
                in stream "ea-dm" you will get confirmation cards: react with a check, or reply "send <code>"
   Threads:     a post in general chat starts a thread (its own topic); reply inside the topic to continue it
   Rooms:       your project rooms are private streams named ea-<project>, threaded the same way
   Briefings:   each briefing and plugin post arrives in "ea" as a topic of its own
   Rollback:    $0 --rollback   (the bot returns to Slack; nothing on the Zulip side is lost)
TXT
