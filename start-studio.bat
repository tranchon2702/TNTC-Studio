@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo   TNTC Studio - AI Video Studio
echo   WebUI: http://127.0.0.1:8501
echo ========================================
uv run streamlit run webui/Main.py --server.address 127.0.0.1 --server.port 8501
pause
