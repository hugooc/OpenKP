# OpenKP on Windows

OpenKP was originally written for macOS and Linux. It runs on Windows too with a couple of extra setup steps. Of 527 tests, 523 pass on Windows. The 4 remaining failures are Linux-specific code paths that don't affect core functionality (reading appointments, labs, messages, visit notes, or any write tool).

## 1. Environment setup

### Visual C++ Redistributable

The `greenlet` package (a transitive Python dependency) fails to load on Windows with a DLL error when the Visual C++ runtime isn't installed. Install it from:

https://aka.ms/vs/17/release/vc_redist.x64.exe

### Reinstall greenlet from a pre-built wheel

After installing the runtime, force-reinstall `greenlet` so pip pulls a pre-built binary instead of compiling from source:

```
.venv\Scripts\pip install --force-reinstall --only-binary=:all: greenlet
```

## 2. Credentials

OpenKP stores Kaiser Permanente credentials in a local `.env` file plus the OS keyring. On Windows:

1. Copy the example file: `copy .env.example .env`
2. Open it in Notepad: `notepad .env`
3. Windows hides dot-files by default. If Notepad won't open it, navigate directly to `C:\Users\<you>\OpenKP\openkp\.env`.
4. Store the password in Windows Credential Manager via `keyring` from inside the venv.

## 3. Known Windows-only test failures

These 4 tests fail on Windows but don't affect any user-facing tool:

| Count | Issue | Detail |
|-------|-------|--------|
| 3 | `%-d` date format | A Linux-only `strftime` directive for stripping leading zeros from day numbers. Windows doesn't support it. |
| 1 | Unix file permissions (`0600`) | Code sets Unix-style file permissions, which don't exist on Windows. |

## 4. Command translation

Same operations, different syntax:

| Purpose | macOS / Linux | Windows |
|---------|---------------|---------|
| Run pytest | `pytest -q` | `.venv\Scripts\pytest -q` |
| Run python | `python script.py` | `.venv\Scripts\python script.py` |
| Run pip | `pip install pkg` | `.venv\Scripts\pip install pkg` |
| Copy a file | `cp file1 file2` | `copy file1 file2` |
| Open a text file | `open file.txt` | `notepad file.txt` |

All commands should be run from `C:\Users\<you>\OpenKP\openkp` in Command Prompt (or PowerShell, with adjustments to slashes).
