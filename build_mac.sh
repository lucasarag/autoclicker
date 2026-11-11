#!/bin/bash
# Gera o app do macOS: dist/AutoClick.app
set -e
cd "$(dirname "$0")"
PY=/opt/homebrew/bin/python3.10
[ -x "$PY" ] || PY=python3

[ -d .venv-build ] || "$PY" -m venv .venv-build
.venv-build/bin/pip install -q pyinstaller

.venv-build/bin/pyinstaller --noconfirm --clean --windowed \
  --name AutoClick \
  --osx-bundle-identifier com.local.autoclicker \
  autoclicker.py

# Limpa atributos estendidos e reassina (ad-hoc) o bundle inteiro; sem isso o
# macOS pode matar o app logo ao abrir (SIGKILL) quando ele e copiado/movido.
xattr -cr dist/AutoClick.app
codesign --force --deep --sign - dist/AutoClick.app
codesign --verify --deep --strict dist/AutoClick.app

# Zip preservando links simbolicos do bundle
rm -f dist/AutoClick-macOS.zip
ditto -c -k --keepParent dist/AutoClick.app dist/AutoClick-macOS.zip

echo
echo "Pronto: $(pwd)/dist/AutoClick.app"
echo "No primeiro uso, conceda Acessibilidade em Ajustes do Sistema > Privacidade e Seguranca."
