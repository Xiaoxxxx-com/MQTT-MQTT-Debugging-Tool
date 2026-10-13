@echo off
chcp 65001 >nul
title MQTT 调试上位机
cd /d "%~dp0"

echo ============================================
echo   MQTT 全功能调试上位机
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8 或更高版本。
    echo        下载地址：https://www.python.org/downloads/
    echo        安装时请勾选 "Add Python to PATH" 和 "tcl/tk and IDLE"。
    echo.
    pause
    exit /b 1
)

python -c "import paho.mqtt.client" >nul 2>nul
if errorlevel 1 (
    echo [提示] 缺少 paho-mqtt 库，正在自动安装...
    python -m pip install paho-mqtt
    if errorlevel 1 (
        echo [错误] 安装失败，请手动执行：pip install paho-mqtt
        pause
        exit /b 1
    )
)

echo [启动] 正在打开调试界面...
echo.
python mqtt_debugger.py

if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出，请查看上方信息。
    pause
)
