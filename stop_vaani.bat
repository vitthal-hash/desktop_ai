@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'VAANI.*main\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Stop command sent.
