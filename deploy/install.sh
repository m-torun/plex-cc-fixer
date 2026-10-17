#!/bin/sh
# Install the daemon as the systemd service plex-cc-fixer, running from this checkout as the user who runs
# this, with its pages and work files in ~/plex-cc-fixer. Arguments are passed on to the daemon:
#   deploy/install.sh --auto "Some Show"
# Run it again after changing them; it restarts the service. Needs sudo.
set -eu
checkout=$(cd "$(dirname "$0")/.." && pwd)
python=$(command -v python3)
unit=/etc/systemd/system/plex-cc-fixer.service
mkdir -p "$HOME/plex-cc-fixer/site" "$HOME/plex-cc-fixer/episodes"
"$python" - "$checkout/deploy/plex-cc-fixer.service" "$(id -un)" "$checkout" "$python" "$HOME/plex-cc-fixer" "$@" \
    > "$checkout/deploy/.unit" <<'PY'
import sys
template, user, checkout, python, data, *options = sys.argv[1:]
def quoted(arg):  # one systemd argument: quotes kept, and % and $ not taken as specifiers or variables
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'
text = open(template, encoding="utf-8").read().split("\n", 1)[1]
for name, value in (("USER", user), ("CHECKOUT", checkout), ("PYTHON", python), ("DATA", data),
                    ("OPTIONS", "".join(" " + quoted(o) for o in options))):
    text = text.replace("@%s@" % name, value)
sys.stdout.write(text)
PY
sudo install -m 644 "$checkout/deploy/.unit" "$unit"
rm -f "$checkout/deploy/.unit"
sudo systemctl daemon-reload
sudo systemctl enable --now plex-cc-fixer >/dev/null 2>&1
sudo systemctl restart plex-cc-fixer
grep '^ExecStart=' "$unit"
systemctl is-active plex-cc-fixer
