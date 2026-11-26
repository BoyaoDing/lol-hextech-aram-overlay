@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动英雄联盟海克斯大乱斗直播实时显示插件...
python -m hextech_overlay --open-browser
pause
