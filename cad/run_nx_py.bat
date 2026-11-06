@echo off
setlocal

if not defined NX_RUN_JOURNAL (
    if not defined UGII_BASE_DIR (
        >&2 echo Set UGII_BASE_DIR or NX_RUN_JOURNAL to your local NX installation.
        exit /b 1
    )
    set "NX_RUN_JOURNAL=%UGII_BASE_DIR%\NXBIN\run_journal.exe"
)
if not exist "%NX_RUN_JOURNAL%" (
    >&2 echo NX journal runner not found: "%NX_RUN_JOURNAL%"
    exit /b 1
)
"%NX_RUN_JOURNAL%" %*
set "RESULT=%ERRORLEVEL%"

endlocal & exit /b %RESULT%
