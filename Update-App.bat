@echo off
title Mk-Tool Updater
echo ========================================================
echo         MK-TOOL - CHECKING FOR LATEST UPDATE
echo ========================================================
echo.
echo GitHub se latest code download kiya ja raha hai...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://github.com/mohdkhan-lab/Mk-tool/archive/refs/heads/main.zip' -OutFile 'update.zip'"

if exist update.zip (
    echo Update extract kiya ja raha hai...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path 'update.zip' -DestinationPath 'temp_update' -Force"
    if exist "app\" (
        xcopy /E /Y /I "temp_update\Mk-tool-main\*" "app\"
    ) else (
        xcopy /E /Y /I "temp_update\Mk-tool-main\*" ".\"
    )
    rmdir /S /Q temp_update
    del update.zip
    echo.
    echo ========================================================
    echo      [SUCCESS] Mk-Tool successfully updated!
    echo ========================================================
) else (
    echo [ERROR] Update download fail ho gaya. Internet connection check karein.
)
pause

