@echo off
setlocal
pushd "%~dp0" || exit /b 1

rem Parse the whole block before pulling, since the pull may update this file.
(
    if /I "%~1"=="--local" (
        echo [1/4] Starting local code without a Git update...
    ) else (
        where git.exe >nul 2>&1 || (
            echo [ERROR] Git is missing. Install Git and reopen this window.
            echo [TIP] To start existing local code, run start-local.bat.
            popd
            pause
            exit /b 1
        )

        echo [1/4] Updating code from the configured Git upstream...
        git pull --ff-only
        if errorlevel 1 (
            echo [ERROR] Update failed. Resolve the Git error above, then retry.
            echo [TIP] To start existing local code, run start-local.bat.
            popd
            pause
            exit /b 1
        )
    )

    if not exist ".venv\Scripts\python.exe" (
        echo Creating the Python 3.12 virtual environment...
        py -3.12 -m venv .venv
        if errorlevel 1 (
            echo [ERROR] Could not create .venv. Install Python 3.12 first.
            popd
            pause
            exit /b 1
        )
    )

    echo [2/4] Checking dependency files...
    ".venv\Scripts\python.exe" -c "import hashlib, pathlib, sys; root = pathlib.Path('.'); digest = hashlib.sha256(b''.join((root / name).read_bytes() for name in ('requirements-runtime.lock', 'pyproject.toml'))).hexdigest(); state = root / '.venv' / '.wxhook-install-state'; sys.exit(0 if state.is_file() and state.read_text(encoding='ascii').strip() == digest else 1)"
    if errorlevel 1 (
        ".venv\Scripts\python.exe" -m pip install -r requirements-runtime.lock
        if errorlevel 1 (
            echo [ERROR] Dependency installation failed.
            popd
            pause
            exit /b 1
        )
        ".venv\Scripts\python.exe" -m pip install --no-deps -e .
        if errorlevel 1 (
            echo [ERROR] Project installation failed.
            popd
            pause
            exit /b 1
        )
    )

    echo [3/4] Checking installed dependencies...
    ".venv\Scripts\python.exe" -m pip check
    if errorlevel 1 (
        echo [ERROR] Dependency check failed. See the details above.
        popd
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -c "import hashlib, pathlib; root = pathlib.Path('.'); digest = hashlib.sha256(b''.join((root / name).read_bytes() for name in ('requirements-runtime.lock', 'pyproject.toml'))).hexdigest(); (root / '.venv' / '.wxhook-install-state').write_text(digest, encoding='ascii')"
    if errorlevel 1 (
        echo [ERROR] Could not save the dependency installation state.
        popd
        pause
        exit /b 1
    )

    if not defined WXHOOK_SYNC_INTERVAL set "WXHOOK_SYNC_INTERVAL=180"
    echo [4/4] Starting wxhook. Keep this window open. Press Ctrl+C to stop.
    echo Management page: http://127.0.0.1:8000/
    ".venv\Scripts\python.exe" main.py
    if errorlevel 1 (
        echo [ERROR] The application exited with an error.
        popd
        pause
        exit /b 1
    )
    popd
    exit /b 0
)
