# Windows local development

## Prerequisites

- Git for Windows
- Python 3.13, 64-bit

Python 3.14 may coexist on the same computer. This project currently uses a Python 3.13 virtual environment so local development and test results remain consistent.

Run these commands from the repository root. A path such as C:\path\to\netease-music-mcp-safe is only an example; never write a user-specific absolute path into project files.

## Create and use the virtual environment

    py -V:3.13 -m venv .venv

Activation is optional. Calling the virtual-environment interpreter directly avoids PowerShell execution-policy issues:

    .\.venv\Scripts\python.exe --version
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    .\.venv\Scripts\python.exe -m pip check
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v

## Protect local and sensitive data

Never commit real environment files, NetEase cookies, access or refresh tokens, passwords, OAuth credentials, SQLite databases, logs, or temporary files. Keep real values outside Git and review git status before every commit.

The repository may keep .env.example as a safe template. Never replace its placeholders with real credentials.
