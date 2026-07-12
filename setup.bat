@echo off
setlocal enabledelayedexpansion

echo ====================================================
echo             STREAMHOME AUTOMATED SETUP WIZARD
echo ====================================================
echo.

:: 1. Create local bin directory
if not exist "bin" mkdir bin

:: 2. Check and Install Python
python --version >nul 2>&1
if "%errorlevel%"=="0" goto :python_installed

echo Python is not installed. Attempting to install...
winget --version >nul 2>&1
if "%errorlevel%"=="0" (
    echo Installing Python 3.11 via winget...
    winget install --id Python.Python.3.11 --exact --source winget --accept-package-agreements --accept-source-agreements
    if "!errorlevel!"=="0" (
        echo Python installed successfully. Please restart your command prompt and run setup.bat again.
        pause
        exit /b 0
    )
)

echo.
echo [ERROR] Python is not installed or not in your PATH.
echo Please download and install Python 3.11 manually from:
echo https://www.python.org/downloads/
echo Make sure to check "Add Python to PATH" during installation.
echo.
pause
exit /b 1

:python_installed
echo Python is ready.

:: 3. Check and Install Node.js
node --version >nul 2>&1
if "%errorlevel%"=="0" goto :node_installed

echo Node.js is not installed. Attempting to install...
winget --version >nul 2>&1
if "%errorlevel%"=="0" (
    echo Installing Node.js via winget...
    winget install --id OpenJS.NodeJS --source winget --accept-package-agreements --accept-source-agreements
    if "!errorlevel!"=="0" (
        echo Node.js installed successfully. Please restart your command prompt and run setup.bat again.
        pause
        exit /b 0
    )
)

echo.
echo [ERROR] Node.js is not installed or not in your PATH.
echo Please download and install Node.js manually from:
echo https://nodejs.org/
echo.
pause
exit /b 1

:node_installed
echo Node.js is ready.

:: 4. Check and Install FFmpeg
ffmpeg -version >nul 2>&1
if "%errorlevel%"=="0" goto :ffmpeg_ready

if exist "C:\ffmpeg\bin\ffmpeg.exe" (
    echo Found FFmpeg at C:\ffmpeg\bin. Copying to bin directory...
    copy /y "C:\ffmpeg\bin\ffmpeg.exe" bin\ >nul
    copy /y "C:\ffmpeg\bin\ffprobe.exe" bin\ >nul 2>&1
    goto :ffmpeg_ready
)
if exist "C:\ffmpeg\ffmpeg.exe" (
    echo Found FFmpeg at C:\ffmpeg. Copying to bin directory...
    copy /y "C:\ffmpeg\ffmpeg.exe" bin\ >nul
    copy /y "C:\ffmpeg\ffprobe.exe" bin\ >nul 2>&1
    goto :ffmpeg_ready
)
if exist "bin\ffmpeg.exe" (
    goto :ffmpeg_ready
)

echo FFmpeg binaries are missing. Downloading FFmpeg Essentials...
if not exist "server\temp" mkdir server\temp
curl.exe -L -o server\temp\ffmpeg.zip https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
if not "%errorlevel%"=="0" (
    echo [ERROR] Failed to download FFmpeg via curl.
    pause
    exit /b 1
)

echo Extracting FFmpeg...
tar.exe -xf server\temp\ffmpeg.zip -C server\temp
if not "%errorlevel%"=="0" (
    echo [ERROR] Failed to extract FFmpeg via tar.
    pause
    exit /b 1
)

:: Move binaries to bin/
for /r server\temp %%f in (ffmpeg.exe ffprobe.exe) do (
    if exist "%%f" copy /y "%%f" bin\ >nul
)

:: Clean up
for /d %%d in (server\temp\ffmpeg-*) do rmdir /s /q "%%d" >nul 2>&1
del /q server\temp\ffmpeg.zip >nul 2>&1
echo FFmpeg installation finished.

:ffmpeg_ready
echo FFmpeg is ready.

:: 5. Check and Install Rclone
rclone version >nul 2>&1
if "%errorlevel%"=="0" goto :rclone_ready

if exist "C:\rclone\rclone.exe" (
    echo Found Rclone at C:\rclone. Copying to bin directory...
    copy /y "C:\rclone\rclone.exe" bin\ >nul
    goto :rclone_ready
)
if exist "bin\rclone.exe" (
    goto :rclone_ready
)

echo Rclone binary is missing. Downloading Rclone...
if not exist "server\temp" mkdir server\temp
curl.exe -L -o server\temp\rclone.zip https://downloads.rclone.org/rclone-current-windows-amd64.zip
if not "%errorlevel%"=="0" (
    echo [ERROR] Failed to download Rclone via curl.
    pause
    exit /b 1
)

echo Extracting Rclone...
tar.exe -xf server\temp\rclone.zip -C server\temp
if not "%errorlevel%"=="0" (
    echo [ERROR] Failed to extract Rclone via tar.
    pause
    exit /b 1
)

:: Move rclone.exe to bin/
for /r server\temp %%f in (rclone.exe) do (
    if exist "%%f" copy /y "%%f" bin\ >nul
)

:: Clean up
for /d %%d in (server\temp\rclone-*) do rmdir /s /q "%%d" >nul 2>&1
del /q server\temp\rclone.zip >nul 2>&1
echo Rclone installation finished.

:rclone_ready
echo Rclone is ready.

:: Ensure bin/ is in the path for python packages
set "PATH=%CD%\bin;%PATH%"

echo.
echo Installing Server Python dependencies...
python -m pip install --upgrade pip
python -m pip install -r server\requirements.txt

echo.
echo Installing Web Client Node dependencies...
cd web
call npm install
cd ..

echo.
echo ====================================================
echo        DEPENDENCIES ARE SUCCESSFULLY INSTALLED
echo            LAUNCHING SETUP CONFIGURATION...
echo ====================================================
echo.
python server\cli.py --setup

pause
