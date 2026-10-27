#!/bin/sh
# Remove o serviço e o programa instalados pelo install.sh.
set -eu
systemctl --user disable --now tabletmonitor.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/tabletmonitor.service" "$HOME/.local/bin/tabletmonitor"
systemctl --user daemon-reload
echo "Removido."
