@echo off
setlocal enabledelayedexpansion
title Carteira BRN P2P
color 0A
cd /d "%~dp0"

:: ============================================================
:: URL DO NGROK (Apenas esta URL sera aberta no navegador)
:: ============================================================
set NGROK_URL=https://seventy-rigging-ploy.ngrok-free.dev

cls
echo.
echo ============================================================
echo    CARTEIRA BRN P2P - Iniciando Sistema
echo ============================================================
echo.

:: ============================================================
:: PASSO 1 - VERIFICA SE JA FOI CONFIGURADO O NAVEGADOR
:: ============================================================
if not exist "navegador.txt" (
    echo    Primeira execucao! Configure o navegador primeiro.
    echo.
    pause
    call CONFIGURAR.BAT
    if not exist "navegador.txt" (
        echo    Configuracao cancelada.
        pause
        exit /b 1
    )
)

:: Le o navegador configurado
set "NAVEGADOR="
for /f "usebackq tokens=*" %%n in ("navegador.txt") do set "NAVEGADOR=%%n"
if "!NAVEGADOR!"=="" set "NAVEGADOR=padrao"

echo    Navegador configurado: !NAVEGADOR!
echo.

:: ============================================================
:: PASSO 2 - VERIFICA DEPENDENCIAS
:: ============================================================
echo [1/4] Verificando Python...
python --version >nul 2>&1
if errorlevel 1 goto SEM_PYTHON
echo    OK
echo.

echo [2/4] Verificando aiohttp...
python -c "import aiohttp" >nul 2>&1
if errorlevel 1 python -m pip install aiohttp --quiet
echo    OK
echo.

echo [3/4] Verificando pyngrok...
python -c "import pyngrok" >nul 2>&1
if errorlevel 1 python -m pip install pyngrok --quiet
echo    OK
echo.

:: ============================================================
:: PASSO 3 - VERIFICA ARQUIVOS DO PROJETO
:: ============================================================
echo [4/4] Verificando arquivos...
if not exist "server.py"         ( echo    ERRO: server.py nao encontrado & pause & exit /b 1 )
if not exist "ngrok_tunnel.py"   ( echo    ERRO: ngrok_tunnel.py nao encontrado & pause & exit /b 1 )
if not exist "index.html"        ( echo    ERRO: index.html nao encontrado & pause & exit /b 1 )
if not exist "app.js"            ( echo    ERRO: app.js nao encontrado & pause & exit /b 1 )
if not exist "config.js"         ( echo    ERRO: config.js nao encontrado & pause & exit /b 1 )
if not exist "ngrok_token.txt"   ( echo    ERRO: ngrok_token.txt nao encontrado & pause & exit /b 1 )
if not exist "ngrok_domain.txt"  ( echo    ERRO: ngrok_domain.txt nao encontrado & pause & exit /b 1 )
echo    OK
echo.

:: ============================================================
:: PASSO 4 - INICIA O SERVIDOR E ABRE O NAVEGADOR
:: ============================================================
echo Iniciando servidor...
echo.
echo ============================================================
echo    SERVIDOR RODANDO LOCALMENTE
echo ============================================================
echo.
echo    Local:       http://localhost:8080
echo    URL Publica: !NGROK_URL!
echo.
echo    O navegador abrira automaticamente na URL PUBLICA em 10 segundos.
echo    Aguarde o Ngrok conectar...
echo.
echo    Para PARAR o servidor: CTRL+C
echo ============================================================
echo.

:: Cria um arquivo temporario que abre o navegador em paralelo
:: sem travar a execucao do servidor.
echo @echo off> _abrir.bat
echo timeout /t 10 /nobreak ^>nul>> _abrir.bat

:: Logica para abrir o navegador configurado ou o padrao
if /i "!NAVEGADOR!"=="padrao" (
    echo start "" "!NGROK_URL!">> _abrir.bat
) else (
    echo if exist "!NAVEGADOR!" (>> _abrir.bat
    echo     start "" "!NAVEGADOR!" "!NGROK_URL!">> _abrir.bat
    echo ^) else (>> _abrir.bat
    echo     start "" "!NGROK_URL!">> _abrir.bat
    echo ^)>> _abrir.bat
)

:: Remove o arquivo temporario apos a execucao
echo del /F /Q "%%~f0" ^>nul 2^>^&1>> _abrir.bat

:: Executa o script de abrir o navegador em paralelo (janela minimizada)
start "" /MIN cmd /c _abrir.bat

:: Inicia o servidor Python (que tambem inicia o Ngrok internamente)
python server.py

echo.
echo Servidor parado.
pause
exit /b 0


:SEM_PYTHON
echo    ERRO: Python nao encontrado!
pause
start https://www.python.org/downloads/
exit /b 1