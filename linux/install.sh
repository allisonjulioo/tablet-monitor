#!/bin/sh
# Instala o programa em ~/.local/bin e o inicia junto com a sessão do GNOME.
set -eu
cd "$(dirname "$0")"
install -Dm755 tabletmonitor.py "$HOME/.local/bin/tabletmonitor"
install -Dm644 tabletmonitor.service "$HOME/.config/systemd/user/tabletmonitor.service"
systemctl --user daemon-reload
systemctl --user enable tabletmonitor.service
systemctl --user restart tabletmonitor.service
echo "Instalado. Registro: journalctl --user -u tabletmonitor -f"
