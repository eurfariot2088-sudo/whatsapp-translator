@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM =============================================================
REM  WhatsAppTranslator 一键打包脚本
REM  在 Windows 上双击或 cmd 中运行即可
REM  产物：dist\WhatsAppTranslator\WhatsAppTranslator.exe
REM =============================================================

cd /d "%~dp0"

echo.
echo [1/5] 检查 Python ...
where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.9+ 并勾选 "Add Python to PATH"
    pause
    exit /b 1
)
python --version

echo.
echo [2/5] 创建虚拟环境 .venv ...
if not exist ".venv" (
    python -m venv .venv
)
call ".venv\Scripts\activate.bat"

echo.
echo [3/5] 安装依赖 + PyInstaller ...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

echo.
echo [4/5] 清理旧产物 ...
if exist "build" rmdir /s /q "build"
if exist "dist"  rmdir /s /q "dist"
if exist "WhatsAppTranslator.spec.bak" del /q "WhatsAppTranslator.spec.bak"

echo.
echo [5/5] 开始打包（onedir 模式，启动快） ...
pyinstaller --noconfirm --clean WhatsAppTranslator.spec

if errorlevel 1 (
    echo.
    echo [错误] 打包失败，请查看上方日志
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  打包成功！
echo  可执行文件位于：dist\WhatsAppTranslator\WhatsAppTranslator.exe
echo  可将整个 dist\WhatsAppTranslator 文件夹拷贝到任意
echo  Windows 电脑上直接运行（无需安装 Python）
echo ============================================================
echo.
echo 如需生成单文件 exe，运行：
echo   build_onefile.bat
echo.

REM 可选：打开输出目录
if exist "dist\WhatsAppTranslator" explorer "dist\WhatsAppTranslator"

pause
endlocal
