@echo off
title LifeReplay Launcher
color 0A

echo.
echo  =============================================
echo    LifeReplay -- Starting Everything
echo  =============================================
echo.

:: ── Check Python ─────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Install from python.org
    pause
    exit /b 1
)
echo  [OK] Python found

:: ── Check Tesseract ───────────────────────────────────────────────────────────
tesseract --version >nul 2>&1
if errorlevel 1 (
    echo  [WARN] Tesseract not in PATH -- trying default install location...
    if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
        set PATH=%PATH%;C:\Program Files\Tesseract-OCR
        echo  [OK] Tesseract found at default location
    ) else (
        echo  [ERROR] Tesseract not found. Run: winget install UB-Mannheim.TesseractOCR
        pause
        exit /b 1
    )
) else (
    echo  [OK] Tesseract found
)

:: ── Install Python deps if needed ────────────────────────────────────────────
echo  [..] Checking Python dependencies...
pip show mss >nul 2>&1
if errorlevel 1 (
    echo  [..] Installing dependencies (first time only, may take a few minutes)...
    pip install -r backend\requirements.txt
    if errorlevel 1 (
        echo  [ERROR] pip install failed. Check your internet connection.
        pause
        exit /b 1
    )
)
echo  [OK] Dependencies ready

:: ── Start Python backend in new window ───────────────────────────────────────
echo  [..] Starting backend (screen capture + API)...
start "LifeReplay Backend" cmd /k "cd /d %~dp0backend && python capture.py"

:: ── Wait for backend to be ready ─────────────────────────────────────────────
echo  [..] Waiting for backend to start...
:wait_loop
timeout /t 2 /nobreak >nul
curl -s http://localhost:5000/api/stats >nul 2>&1
if errorlevel 1 (
    echo  [..] Still waiting...
    goto wait_loop
)
echo  [OK] Backend is ready at http://localhost:5000

:: ── Serve frontend and open browser ──────────────────────────────────────────
echo  [..] Starting frontend server...
start "LifeReplay Frontend" cmd /k "cd /d %~dp0frontend && python -m http.server 8080"

timeout /t 1 /nobreak >nul

echo  [OK] Opening browser...
start http://localhost:8080

echo.
echo  =============================================
echo    LifeReplay is running!
echo.
echo    Frontend  -->  http://localhost:8080
echo    Backend   -->  http://localhost:5000
echo    Data dir  -->  %USERPROFILE%\LifeReplay
echo.
echo    Close this window to keep running.
echo    Close the Backend window to stop recording.
echo  =============================================
echo.
pause
