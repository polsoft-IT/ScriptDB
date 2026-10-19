# ScriptDB portable build

Everything needed to turn the project into **one self-contained Windows
executable**: `dist/ScriptDB.exe`. No installer, no Python on the target
machine, nothing written to the registry.

```
builder/
├── build.bat          the build driver (run this)
├── ScriptDB.spec      PyInstaller recipe: one file, icon, version, excludes
├── version_info.txt   the VERSIONINFO resource embedded in the exe
├── sdb.ico            the application icon (14 embedded sizes)
└── dist/
    └── ScriptDB.exe   the result, ~38 MB
```

The `build/` work folder PyInstaller creates is **deleted again when the build
finishes**, so only the sources and the exe are left behind.

---

## Build

```bat
builder\build.bat
```

| Argument | Effect |
|---|---|
| *(none)* | Clean build. Removes the previous `dist/` and `build/`. |
| `/fast` | Keeps the `build/` work folder, so a rebuild is a little quicker. |
| `/nopause` | Does not wait for a key at the end. Use this in CI. |

The script does five steps and prints a summary at the end:

1. removes the previous output,
2. runs `_scan.py`, the project's own parse check,
3. runs PyInstaller,
4. verifies the exe exists and reads its size,
5. deletes the `build/` work folder and any `__pycache__` folders.

### Requirements

- **Windows**
- **Python 3** with **PyInstaller**: `python -m pip install pyinstaller`
- The spec's interpreter probe accepts any Python that can `import PyInstaller`,
  so a second install without it is skipped automatically.

---

## What goes into the exe

| Included | Why |
|---|---|
| `sdb_scripts.py` | The program itself. |
| `app_paths.py` | Owns the application data folder. Imported by name, so it is a hidden import in the spec. |
| `tabs/` (all modules) | The six tabs, the editor, the search engine, the navigation engine. |
| `tabs/syntax/` | All 13 language modules. They are imported dynamically by the registry, so the spec lists the whole folder. |
| `sdb.ico` | The running program loads it to set the window icon. |
| The databases in `app_paths.SEED_FILES` | Bundled as *read-only defaults*. On first run they are copied into the application data folder; a file that is already there is never overwritten. |

`excludes` in the spec drops the parts of PyQt5 this app never imports
(WebEngine, Multimedia, QML, SQL, OpenGL, …) plus `tkinter`, `numpy`, `PIL` and
friends. That is what keeps the exe at ~38 MB instead of well over 100 MB.

---

## How the program finds its files

`PyInstaller` unpacks a one-file exe into a temporary folder and points
`sys._MEIPASS` at it. `sdb_scripts.py` handles both cases:

```python
def _resource_base_dir():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS        # running from the exe
    return BASE_DIR                # running from source
```

`app_icon_path()` then looks for `sdb.ico` in `_MEIPASS`, in `builder/`, and in
the source folder, and returns the first one it finds — so the icon works in
both modes, and the build still succeeds if the icon is missing (it warns and
carries on).

**Where the data goes.** `_MEIPASS` is deleted when the exe exits, so nothing may
be written there and nothing may be written next to the exe either. `app_paths.py`
is the single module that owns the location, and it is bundled inside the exe as
a hidden import. The two folders are fixed in that module and belong to the user
who runs the exe:

```python
%APPDATA%\.polsoft\ScriptDB\        # what the program produces
%APPDATA%\.polsoft\ScriptDB\db\     # the databases
```

`%APPDATA%` already resolves to `C:\Users\<userprofile>\AppData\Roaming`, so a
local account, a domain account and a redirected profile all land in the right
place — the file can be handed to another user.

The databases — `scripts_db.json`, `settings_db.json`, `connections_db.json`,
`todo_db.json`, `shopping_db.json`, `apps_db.json`, `debugger_db.json`,
`batch_templates.json`, plus `docs_db.sqlite`, the audit log and timeline, and
the `vault\`, `security\` and `structure\` subfolders — live in `db\`. Reports,
exports, logs and generated snapshots live in the folder above it.

`build.bat` refuses to start without `app_paths.py`, and the spec's
`pathex=[PROJECT_DIR]` puts the project folder on PyInstaller's import path, so
the hidden import resolves during analysis rather than at the user's first run.
PyInstaller rejects `--paths` when a `.spec` file is given, which is why the path
is set inside the spec.

---

## Distributing

1. Copy `builder/dist/ScriptDB.exe` wherever you like and run it — including
   `C:\Program Files`, because the exe itself writes nothing.
2. Everything the program creates goes to
   `%APPDATA%\.polsoft\ScriptDB`, with the databases in its `db` subfolder. Both
   are created on first start, together with the default databases bundled in
   the exe.
3. No install step, no uninstaller: delete the exe to remove the program, and
   delete that one data folder to remove everything it wrote.

> **SmartScreen.** A brand-new unsigned exe usually triggers "Windows protected
> your PC". Users click *More info* → *Run anyway*. To avoid that, sign the exe:
>
> ```bat
> signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
>          /f mycert.pfx /p MYPASSWORD builder\dist\ScriptDB.exe
> ```
>
> Add the `signtool` call at the end of `build.bat` if you have a certificate.

---

## Versioning

Two places carry the version, and they must be bumped together:

| File | Field |
|---|---|
| `builder/version_info.txt` | `filevers`, `prodvers`, `FileVersion`, `ProductVersion` |
| `builder/build.bat` | `APP_VERSION` (shown in the summary) |

`version_info.txt` ends up under **Properties → Details** in Windows Explorer.

### Why `version_info.txt` and not a `.py` file

PyInstaller does not import the version file — it reads the text and runs
`eval()` on it. The file therefore has to be **exactly one expression**:

- no `import` (a statement, not an expression),
- no docstring,
- no `print`,
- no trailing statements.

The names `VSVersionInfo`, `FixedFileInfo`, … are supplied by `ScriptDB.spec`,
which imports them into its own namespace before PyInstaller evaluates the file.
A `.py` file with an import line fails with:

```
Failed to deserialize VSVersionInfo from text-based representation
```

Keep it a plain `.txt` with a single call in it.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No Python interpreter with PyInstaller was found` | `python -m pip install pyinstaller`, or run from a prompt where that Python is on PATH. |
| `Failed to deserialize VSVersionInfo` | `version_info.txt` is not a single expression — remove any statement from it. |
| `unexpected indent` / `invalid syntax` during the build | Same cause as above, or a Python file in `tabs/` no longer parses. Run `python _scan.py` first. |
| The exe starts but shows no window | Something failed during start-up. Rebuild without `console=False` in the spec to see the traceback. |
| `app_paths.py not found` from `build.bat` | The module that owns the data folder is missing from the project root. Restore it; without it the exe would write next to itself. |
| `ScriptDB.exe writes next to itself` | `app_paths` was not bundled. Check that `"app_paths"` is in the spec's `hiddenimports`. |
| `Access is denied` when saving a script | `%APPDATA%` is not writable for this user. |
| The exe is much larger than expected | `upx` is not installed, or the `excludes` list was edited. UPX compression is optional; the build works without it. |
| Antivirus quarantines the exe | Common for unsigned PyInstaller output. Sign it, or whitelist it. |

---

## Verified

The build was run end to end on this project and produced:

```
Status         : SUCCESS
Executable     : builder\dist\ScriptDB.exe
Size           : ~38 MB
Icon embedded  : builder\sdb.ico
Requires Python: no
Work folder    : builder\build\ removed
```

The resulting exe was then checked standalone:

- `ScriptDB.exe --help` → prints the CLI help, exit 0.
- `ScriptDB.exe a.py b.py --no-gui --format unified` → prints a unified diff.
- `ScriptDB.exe a.py b.py --stats` → prints the diff statistics.
- Starting it with no arguments → the GUI window stays up.
- `Properties → Details` → shows `2.5.0.0`, "ScriptDB", the description and the
  copyright from `version_info.txt`.
