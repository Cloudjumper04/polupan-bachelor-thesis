# Local diagnostic scripts

These scripts are manual local/demo diagnostics for checking a running SmartEnergy Lab demo environment. They are not part of normal application operation or scheduled backend maintenance.

Run them from the project root:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\diagnostics\check_api_smoke.ps1
powershell -ExecutionPolicy Bypass -File .\tools\diagnostics\check_full_system.ps1
powershell -ExecutionPolicy Bypass -File .\tools\diagnostics\check_grid_history_api.ps1
```

Expected local ports:

- backend API: `6001`
- frontend: `5173`

`check_full_system.ps1` is diagnostic-only. It reads the runtime SQLite database for integrity checks and should not be run during active database mutation, data refresh, scheduler work, or other write-heavy operations.
