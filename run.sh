#!/bin/bash
export TK_SILENCE_DEPRECATION=1
cd "$(dirname "$0")" || exit 1

# Prefere o Python do Homebrew (Tk 8.6, sem o bug de janela cinza do Tk 8.5 do sistema)
for PY in /opt/homebrew/bin/python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$PY" ] && "$PY" -c "import tkinter" 2>/dev/null; then
    exec "$PY" autoclicker.py
  fi
done
echo "Nenhum Python com tkinter encontrado." >&2
exit 1
