@echo off
rem Simulador interativo do pedidos-service. Uso: simular
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0simular.ps1" %*
