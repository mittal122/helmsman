@echo off
setlocal enabledelayedexpansion
REM ===========================================================================
REM  Helmsman Windows bootstrap. Installs ONLY what's missing (Docker Desktop,
REM  kubectl, minikube, helm) via winget, starts a local cluster, and runs the
REM  app container. Idempotent: present -> skipped, missing -> installed.
REM
REM  NOTE: the most reliable Windows path is WSL2 (Docker Desktop uses it anyway):
REM        install WSL2 + Ubuntu, then run  ./setup.sh  inside it. This .bat is the
REM        native alternative.
REM ===========================================================================
echo === Helmsman Windows bootstrap ===

where winget >nul 2>&1
if errorlevel 1 ( echo [fatal] winget not found. Needs Windows 10/11 with "App Installer" from the Microsoft Store. & exit /b 1 )

REM ---- 1. Docker Desktop ----
where docker >nul 2>&1
if errorlevel 1 (
  echo Installing Docker Desktop...
  winget install -e --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
  echo. & echo Docker Desktop installed. START IT once ^(so the daemon runs^), then re-run this script. & exit /b 0
) else ( echo [ok] docker present )

docker info >nul 2>&1
if errorlevel 1 ( echo [fatal] Docker Desktop is installed but not running. Start it, then re-run. & exit /b 1 )
echo [ok] docker daemon running

REM ---- 2. kubectl ----
where kubectl >nul 2>&1
if errorlevel 1 ( echo Installing kubectl... & winget install -e --id Kubernetes.kubectl --accept-source-agreements --accept-package-agreements ) else ( echo [ok] kubectl present )

REM ---- 3. minikube ----
where minikube >nul 2>&1
if errorlevel 1 ( echo Installing minikube... & winget install -e --id Kubernetes.minikube --accept-source-agreements --accept-package-agreements ) else ( echo [ok] minikube present )

REM ---- 4. helm (host copy optional; the app image already bundles it) ----
where helm >nul 2>&1
if errorlevel 1 ( echo Installing helm... & winget install -e --id Helm.Helm --accept-source-agreements --accept-package-agreements ) else ( echo [ok] helm present )

echo. & echo If any tool was just installed, CLOSE and REOPEN this window ^(PATH refresh^), then re-run.

REM ---- 5. start a local cluster (embed-certs -> self-contained kubeconfig) ----
minikube status >nul 2>&1
if errorlevel 1 ( echo Starting minikube... & minikube start --driver=docker --embed-certs )
echo [ok] cluster running

REM ---- 6. get the app image (load the shipped tar, else build from source) ----
docker image inspect helmsman:1.0 >nul 2>&1
if errorlevel 1 (
  if exist helmsman-image.tar.gz ( echo Loading image from tar... & docker load -i helmsman-image.tar.gz
  ) else if exist Dockerfile ( echo Building image... & docker build -t helmsman:1.0 .
  ) else ( echo [fatal] No helmsman:1.0 image, no helmsman-image.tar.gz, no Dockerfile. & exit /b 1 )
)

REM ---- 7. make the kubeconfig reachable from inside the container ----
REM Docker Desktop maps the host as host.docker.internal; rewrite the API address.
powershell -NoProfile -Command "(Get-Content \"$env:USERPROFILE\.kube\config\") -replace '127\.0\.0\.1','host.docker.internal' -replace 'https://localhost','https://host.docker.internal' | Set-Content \"$env:TEMP\helmsman-kubeconfig\""

REM ---- 8. run the app ----
docker rm -f helmsman >nul 2>&1
docker run -d --name helmsman -p 8000:8000 --add-host host.docker.internal:host-gateway ^
  -e ALLOW_OPEN_DEV=1 -e COOKIE_INSECURE=1 -e KUBECONFIG=/kube/config ^
  -v "%TEMP%\helmsman-kubeconfig:/kube/config:ro" helmsman:1.0

echo. & echo === Helmsman running -> http://localhost:8000 ===
start http://localhost:8000
endlocal
