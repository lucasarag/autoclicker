@echo off
REM Gera o executavel do Windows: dist\AutoClicker.exe
REM Requisitos: Python 3.9+ para Windows (com tkinter) instalado.
cd /d "%~dp0"

if not exist .venv-build (
  python -m venv .venv-build || goto :err
)
call .venv-build\Scripts\activate.bat
pip install -q pyinstaller || goto :err

pyinstaller --noconfirm --clean --onefile --windowed --name AutoClicker autoclicker.py || goto :err

echo.
echo Pronto: %cd%\dist\AutoClicker.exe
exit /b 0

:err
echo Falha ao gerar o executavel.
exit /b 1
