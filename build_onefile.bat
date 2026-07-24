@echo off
chcp 65001 >nul
echo ===============================================
echo  WhatsApp Translator 打包脚本 (单文件模式)
echo ===============================================

if not exist .venv (
    echo [1/4] 创建虚拟环境...
    python -m venv .venv
)

echo [2/4] 激活虚拟环境并安装依赖...
call .venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller

echo [3/4] 打包...
pyinstaller --noconfirm --clean --onefile WhatsAppTranslator.spec

echo [4/4] 完成!
echo 产物: dist\WhatsAppTranslator.exe
pause
