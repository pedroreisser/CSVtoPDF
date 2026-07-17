@echo off
setlocal enabledelayedexpansion
title CSVtoPDF
cd /d "%~dp0"

echo.
echo  ================================================
echo  CSVtoPDF - Download de artigos em acesso aberto
echo  ================================================
echo.
echo  Nao e necessario ser administrador.
echo.

:: ── 1. Procurar Python instalado ─────────────────────────────────────────────
set PYTHON=

:: Tentar "python"
python --version >nul 2>&1
if !errorlevel! equ 0 (
    set PYTHON=python
    goto :PYTHON_ENCONTRADO
)

:: Tentar "py" (launcher do Windows)
py --version >nul 2>&1
if !errorlevel! equ 0 (
    set PYTHON=py
    goto :PYTHON_ENCONTRADO
)

:: Tentar "python3"
python3 --version >nul 2>&1
if !errorlevel! equ 0 (
    set PYTHON=python3
    goto :PYTHON_ENCONTRADO
)

:: ── 2. Python nao encontrado: instalar via winget (sem admin) ────────────────
echo  Python nao encontrado. Instalando automaticamente...
echo  (O Windows pode pedir confirmacao, mas NAO e necessario admin)
echo.

:: winget esta disponivel no Windows 10/11
winget --version >nul 2>&1
if !errorlevel! equ 0 (
    echo  Instalando Python via winget (usuario atual, sem admin)...
    winget install --id Python.Python.3 --source winget ^
        --scope user ^
        --accept-package-agreements ^
        --accept-source-agreements ^
        --silent
    if !errorlevel! equ 0 (
        echo  Python instalado com sucesso!
        echo.
        :: Recarregar PATH desta sessao
        call refreshenv >nul 2>&1
        :: Tentar novamente apos instalacao
        python --version >nul 2>&1
        if !errorlevel! equ 0 (
            set PYTHON=python
            goto :PYTHON_ENCONTRADO
        )
        py --version >nul 2>&1
        if !errorlevel! equ 0 (
            set PYTHON=py
            goto :PYTHON_ENCONTRADO
        )
        echo.
        echo  Python foi instalado mas requer reiniciar o computador
        echo  para atualizar o PATH. Reinicie e execute este arquivo novamente.
        pause
        exit /b 0
    )
)

:: winget nao disponivel ou falhou — opcoes manuais
echo.
echo  Nao foi possivel instalar automaticamente.
echo  Escolha uma opcao:
echo.
echo    [1] Abrir Microsoft Store (mais facil, sem admin)
echo    [2] Abrir site python.org para download manual
echo    [3] Sair
echo.
set /p OPCAO=" Digite o numero e pressione Enter: "

if "%OPCAO%"=="1" (
    echo  Abrindo Microsoft Store...
    start ms-windows-store://pdp/?ProductId=9NCVDN91XZQP
    echo  Instale o Python, reinicie e execute este arquivo novamente.
    pause
    exit /b 0
)
if "%OPCAO%"=="2" (
    echo  Abrindo python.org...
    start https://www.python.org/downloads/
    echo  IMPORTANTE: marque "Add Python to PATH" durante a instalacao.
    echo  Depois reinicie e execute este arquivo novamente.
    pause
    exit /b 0
)
echo  Saindo.
exit /b 0

:: ── 3. Python encontrado: verificar versao minima ────────────────────────────
:PYTHON_ENCONTRADO
for /f "tokens=2" %%v in ('!PYTHON! --version 2^>^&1') do set PYVER=%%v
echo  Python !PYVER! encontrado.

:: Extrair versao major.minor para verificar 3.9+
for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
    set PY_MAJ=%%a
    set PY_MIN=%%b
)
if !PY_MAJ! lss 3 goto :PYTHON_VELHO
if !PY_MAJ! equ 3 if !PY_MIN! lss 9 goto :PYTHON_VELHO
goto :PYTHON_OK

:PYTHON_VELHO
echo.
echo  Versao muito antiga (requer 3.9+).
echo  Instale a versao mais recente em: https://python.org/downloads
pause
exit /b 1

:PYTHON_OK

:: ── 4. Atualizar pip silenciosamente ─────────────────────────────────────────
!PYTHON! -m pip install --upgrade pip --user --quiet 2>nul

:: ── 5. Abrir o launcher do CSVtoPDF ─────────────────────────────────────
echo.
echo  Iniciando CSVtoPDF...
echo.
!PYTHON! iniciar.py
if !errorlevel! neq 0 (
    echo.
    echo  Ocorreu um erro ao iniciar o CSVtoPDF.
    echo  Tente executar manualmente no terminal:
    echo    python iniciar.py
    echo.
    pause
)

endlocal
