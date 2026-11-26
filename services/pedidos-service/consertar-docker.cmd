@echo off
rem Conserta o Docker Desktop que fecha ao iniciar (socket antigo). Uso: consertar-docker
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0consertar-docker.ps1" %*
