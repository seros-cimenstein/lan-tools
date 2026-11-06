#!/usr/bin/env bash
# ./install.sh [install|uninstall]   — run the grand_viewer dashboard as a user service.
# Nothing is installed on the monitored machines: the hub reaches them over ssh.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

LIB=~/.local/lib/grand_viewer
CONF=~/.config/grand_viewer/config.json
UNIT=~/.config/systemd/user/grand_viewer.service
FISH_FN=~/.config/fish/functions/grand-viewer.fish

case ${1:-install} in
install)
	install -Dm644 -t "$LIB" probe.py index.html
	install -Dm755 -t "$LIB" grand_viewer.py grand-viewer
	install -Dm644 grand-viewer.fish "$FISH_FN"
	install -Dm644 grand_viewer.service "$UNIT"
	if [[ ! -e $CONF ]]; then
		install -Dm644 config.example.json "$CONF"
		echo "wrote $CONF (edit hosts there)"
	fi
	systemctl --user daemon-reload
	systemctl --user enable grand_viewer.service
	systemctl --user restart grand_viewer.service
	python3 - "$CONF" <<-'EOF'
		import json, sys
		c = json.load(open(sys.argv[1]))
		print("dashboard: http://%s:%d/ (or run: grand-viewer)" % (c.get("listen", "127.0.0.1"), c.get("port", 47800)))
	EOF
	;;
uninstall)
	systemctl --user disable --now grand_viewer.service 2>/dev/null || true
	rm -f "$UNIT" "$FISH_FN"
	rm -rf "$LIB"
	systemctl --user daemon-reload
	echo "uninstalled (config kept at $CONF)"
	;;
*)
	echo "usage: $0 [install|uninstall]" >&2
	exit 2
	;;
esac
