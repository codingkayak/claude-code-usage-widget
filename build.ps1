# Packages the widget into a standalone .exe (no Python install needed to run it).
# Run from this folder: .\build.ps1

python -m pip install -r requirements.txt pyinstaller

pyinstaller --onefile --windowed --noconfirm `
    --name "claude-usage-widget" `
    tray_widget.py

Write-Host ""
Write-Host "Build output: dist\claude-usage-widget.exe"
Write-Host ""
Write-Host "To have it start with Windows, create a shortcut to the .exe above in the Startup folder:"
Write-Host "  1. Win+R -> shell:startup -> Enter"
Write-Host "  2. Paste a shortcut to dist\claude-usage-widget.exe in that folder"
