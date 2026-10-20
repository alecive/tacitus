# mediator-sandbox.sh: the systemd sandbox of ea-mail-mediator.service.
# Sourced, not run: deploy/enable-mail-mediation.sh puts these lines in the
# unit it writes, and deploy/harden-permissions.sh in the drop-in that
# points the unit at /opt, so both carry the same sandbox.
#
#     mediator_sandbox_dirs <owner> <owner home>     # create the write roots
#     mediator_sandbox <owner home> <ea-bot home>    # print the [Service] lines
#
# The mediator runs as the owner and holds the mail tokens, so a fault in it,
# or in a tool it runs, reaches whatever the owner may write. The sandbox
# makes the whole file system read-only except:
#   * the resident's home: the request queue, the token files, and every
#     root a mediated file may be delivered to. The resident owns all of it
#     already. Its home is named, not a directory inside it, because systemd
#     follows a symlink in ReadWritePaths= and /home/ea-bot is the one entry
#     there the resident cannot replace with one;
#   * /tmp, an allowed root the resident shares with the mediator;
#   * the owner-private directory (logs, secrets, plugin state, the remote
#     writer's store) and the mediator's staging root;
#   * sudo's own state directories, written by the setuid sudo only.
# The owner's ~/.ssh and ~/.gnupg are hidden.
#
# NoNewPrivileges= stays off: the copy of a resident-named file in and out of
# staging runs through `sudo -u ea-bot`, and no_new_privs stops sudo. The
# options that imply it for a unit with User= stay off for the same reason
# (SystemCallFilter=, SystemCallArchitectures=, RestrictAddressFamilies=,
# RestrictNamespaces=, PrivateDevices=, ProtectKernelModules=,
# ProtectKernelLogs=, ProtectClock=, MemoryDenyWriteExecute=,
# RestrictRealtime=, RestrictSUIDSGID=, LockPersonality=), and so do
# ProtectKernelTunables= and ProtectControlGroups=: the owner cannot write
# /proc/sys or the cgroup tree anyway, so they add nothing that is worth a
# risk to the sudo drop. PrivateTmp= stays
# off: /tmp is an allowed root, and a private /tmp would read and deliver
# files the resident never sees. RemoveIPC= stays off: it removes every IPC
# object of the owner when the unit stops. ProtectProc= stays off: the deck
# route check reads the resident's listener in /proc.

mediator_sandbox_dirs() {
    install -d -o "$1" -g "$(id -gn "$1")" -m 700 "$2/.ea-private" "$2/.ea-mediator-staging"
}

mediator_sandbox() {
    cat <<EOF
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$2 /tmp $1/.ea-private $1/.ea-mediator-staging
ReadWritePaths=-/run/sudo -/run/sudo-rs -/var/lib/sudo
InaccessiblePaths=-$1/.ssh -$1/.gnupg
EOF
}
