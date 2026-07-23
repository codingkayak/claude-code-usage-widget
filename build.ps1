# Empacota o widget num .exe standalone (nao precisa de Python instalado pra rodar).
# Rode a partir desta pasta: .\build.ps1

python -m pip install -r requirements.txt pyinstaller

pyinstaller --onefile --windowed --noconfirm `
    --name "claude-usage-widget" `
    tray_widget.py

Write-Host ""
Write-Host "Build feito em: dist\claude-usage-widget.exe"
Write-Host ""
Write-Host "Pra rodar junto com o Windows, cria um atalho do .exe acima na pasta Startup:"
Write-Host "  1. Win+R -> shell:startup -> Enter"
Write-Host "  2. Cola um atalho de dist\claude-usage-widget.exe nessa pasta"
