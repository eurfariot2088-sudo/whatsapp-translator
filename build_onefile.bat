@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM =============================================================
REM  WhatsAppTranslator 单文件打包脚本（生成单个 .exe）
REM  产物：dist\WhatsAppTranslator.exe
REM  优点：分发方便，只有一个文件
REM  缺点：启动比 onedir 模式慢 2~3 秒
REM =============================================================

cd /d "%~dp0"

echo.
echo [1/4] 激活虚拟环境 ...
if not exist ".venv" (
    echo [错误] 请先运行 build.bat 创建虚拟环境
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"

echo.
echo [2/4] 检查 PyInstaller ...
python -c "import PyInstaller" 2>nul
if errorlevel 1 python -m pip install pyinstaller

echo.
echo [3/4] 清理旧产物 ...
if exist "build" rmdir /s /q "build"
if exist "dist\WhatsAppTranslator.exe" del /q "dist\WhatsAppTranslator.exe"

echo.
echo [4/4] 开始单文件打包 ...
pyinstaller --noconfirm --clean --onefile ^
    --name "WhatsAppTranslator" ^
    --windowed ^
    --collect-all deep_translator ^
    --collect-all pystray ^
    --collect-all PIL ^
    --collect-submodules keyboard ^
    --collect-submodules uiautomation ^
    --collect-submodules comtypes ^
    --hidden-import "deep_translator.google" ^
    --hidden-import "pystray._win32" ^
    --hidden-import "pystray._util" ^
    main.py

if errorlevel 1 (
    echo.
    echo [错误] 打包失败
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  打包成功！
echo  单文件：dist\WhatsAppTranslator.exe
echo  拷贝到任意 Windows 电脑即可运行
echo ============================================================
echo.

if exist "dist\WhatsAppTranslator.exe" explorer "dist"

pause
endlocal
