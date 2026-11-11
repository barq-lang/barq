@echo off
rem Barq launcher for Windows: runs the reference interpreter with Python 3.
set "BARQ_HOME=%~dp0.."
set "PYTHONPATH=%BARQ_HOME%;%PYTHONPATH%"
python -m barq %*
