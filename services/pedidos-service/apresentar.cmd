@echo off
rem Roteiro de apresentação do pedidos-service. Uso: apresentar [-SemPausa] [-Reiniciar]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0apresentar.ps1" %*
