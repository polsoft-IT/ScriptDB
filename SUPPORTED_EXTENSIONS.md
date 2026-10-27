# ScriptDB — obsługiwane rozszerzenia skryptów

Ten plik zbiera **wszystkie rozszerzenia plików skryptów, które rozpoznaje ScriptDB**,
oraz wyjaśnia, na jakiej zasadzie każde z nich jest mapowane na kategorię skryptu
i—w konsekwencji—na konkretny moduł języka w poszczególnych warstwach aplikacji.

---

## 1. Zasada ogólna (jedno źródło prawdy na warstwę)

ScriptDB nie ma jednej globalnej listy rozszerzeń. Każda warstwa ma własną tabelę,
bo każda potrzebuje czegoś innego, ale **wszystkie posługują się tym samym
schematem**: *rozszerzenie → kategoria skryptu → moduł języka*.

| Warstwa | Plik | Co decyduje | Do czego służy |
|---|---|---|---|
| Baza / import / eksport | `sdb.py` (`CATEGORY_EXTENSIONS`, `EXTENSION_ALIASES`) | kategoria skryptu (`CATEGORIES`) | naturalny plik źródłowy przy imporcie i eksporcie |
| Podświetlanie składni (Editor) | `tabs/syntax/*.py` + `tabs/syntax/registry.py` | `SyntaxModule.extensions` | kolory, słowa kluczowe, komentarze |
| Debugger (adaptery języków) | `tabs/debug_langs.py` | `LanguageAdapter.extensions` | bloki, breakpointy, frames, walidacja składni |
| Piaskownica (uruchamianie) | `tabs/sandbox_langs.py` (`INTERPRETERS`) | alias języka skryptu | wybór interpretera, flagi ograniczeń, sufiks pliku |
| Skaner metadanych | `tabs/metadata_core.py` (`script_extensions()`) | zlepienie `debug_langs` + lista dodatkowa | które pliki w ogóle skanować |
| Detekcja języka w treści | `sdb.py` (`LANGUAGE_SIGNATURES`, `guess_category`) | rozszerzenie **lub** wzorce w kodzie | podpowiedź języka, gdy brak kategorii |
| Repozytorium DOCs | `tabs/docs_types.py` (`DOCUMENT_TYPES`) | typ dokumentu | traktowanie skryptu jako *dokumentu* |

### Kolejność rozstrzygania (identyczna w rejestrach)

1. **Dokładne rozszerzenie** (małe litery, z kropką) — wygrywa zawsze.
2. **Alias rozszerzenia** — drugorzędne pisownie (`.yml`, `.ts`, `.cxx`, `.cmd` ...).
3. **Nazwa kategorii / alias języka** — dopasowanie po kluczu, a potem po
   podłańcuchu (np. `"Registry (REG)"` → `system`, `"HTA"` → `html`).
4. **Fallback** — nigdy `None`:
   - rejestr składni → `python` (`DEFAULT_KEY`),
   - adapter debuggera → `python`,
   - piaskownica → `python` (najsilniejsze ograniczenia in-process),
   - baza → kategoria `"Other"` i rozszerzenie `.txt`.

Wyszukiwanie rozszerzenia jest **niewrażliwe na wielkość liter**
(`APP.PY` = `app.py`), a plik bez kropki trafia od razu do fallbacku.

### Rozszerzenie to nie to samo co „obsługiwany język”

Trzy różne rzeczy mogą się różnić dla jednego pliku i to jest zamierzone:

- **kategoria w bazie** — co użytkownik wybrał (lista `CATEGORIES`),
- **adapter/debugger** — co aplikacja umie *przeanalizować*,
- **interpreter w piaskownicy** — co da się *uruchomić*.

Przykład: `.kt` (Kotlin) ma kategorię przez alias składni do `java`, ale
piaskownica nie ma interpretera Kotlina — `resolve("java")` (bo alias to `java`)
uruchomi JVM, o ile jest dostępny, a raport piaskownicy wprost powie, co zostało
użyte.

---

## 2. Rozszerzenia skryptów rozpoznawane przez bazę (`sdb.py`)

Zasada: `CATEGORY_EXTENSIONS` to **kanoniczna** para kategoria → jedno rozszerzenie
(używana przy eksporcie; brak wpisu = `.txt`). `EXTENSION_ALIASES` dodaje
**drugorzędne pisownie**, które mapują się na istniejącą kategorię i nigdy nie
tworzą nowej.

| Kategoria | Rozszerzenie kanoniczne | Aliasy rozszerzeń | Uwagi |
|---|---|---|---|
| Python | `.py` | `.pyw`, `.pyi`, `.pyx` | `.pyi` to stub, `.pyw` — GUI bez konsoli. W systemie Windows pliki `.py` są automatycznie uruchamiane przez oficjalny launcher Pythona `py.exe` w zakładce Apps. |
| Bash | `.sh` | `.bash`, `.zsh`, `.ksh`, `.shell` | dowolna powłoka POSIX → ta sama kategoria |
| PowerShell | `.ps1` | `.psm1`, `.psd1` | moduł i manifest modułu |
| VBScript | `.vbs` | `.vbe`, `.wsf` | `.wsf` to plik skryptu Windows Script Host |
| JavaScript | `.js` | `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx` | TypeScript celowo ląduje tutaj |
| Java | `.java` | — | Kotlin/Groovy tylko w warstwie składni (`.kt`) |
| C++ | `.cpp` | `.c`, `.cc`, `.cxx`, `.hpp`, `.h` | `.h` traktowany jako C++ |
| Batch | `.bat` | `.cmd`, `.nt` | |
| HTML | `.html` | `.htm`, `.xhtml` | |
| HTA | `.hta` | — | osobna kategoria, choć składnia to HTML |
| Registry (REG) | `.reg` | `.reg` | tylko pliki rejestru Windows |
| JSON | `.json` | `.jsonc`, `.json5` | |
| YAML | `.yaml` | `.yml` | |
| INI | `.ini` | `.toml` | TOML pod INI (klucz = wartość) |
| System config | `.cfg` | `.conf`, `.inf`, `.env` | |
| SQL | `.sql` | `.sqlite`, `.sqlite3` | pliki bazy też otwierają się jako SQL |
| Ruby | `.rb` | `.rake` | |
| Other | `.txt` | — | wszystko, czego nie znamy |

> **Uwaga o kolizjach:** klucz może być w jednej tabeli tylko raz. Dlatego
> `.c` i `.h` są w C++, `.kt` w składni trafia do Javy, a `.toml` do INI — decyduje
> **jedna** tabela na warstwę, a nie suma wszystkich pomysłów.

---

## 3. Warstwa składni — `tabs/syntax/` (13 modułów)

Zasada: każdy język to klasa `SyntaxModule` z polami `key`, `label`, `aliases`,
`extensions`. Rejestr (`SyntaxRegistry`) buduje z nich dwie mapy:
`alias → moduł` i `rozszerzenie → moduł`, a przy rejestracji **nowszy moduł
wygrywa** (stare aliasy i rozszerzenia są przycinane przez `_prune()`).
Moduł można dodać, podmienić i usunąć w czasie działania programu
(`register_module` / `replace_module` / `unregister_module` / `load_plugin`).

| Klucz modułu | Etykieta | Rozszerzenia | Aliasy kategorii |
|---|---|---|---|
| `python` | Python | `.py` | `python`, `py` |
| `cpp` | C++ | `.cpp`, `.hpp`, `.cc` | `c++`, `cpp`, `cplus`, `cxx`, `c` |
| `java` | Java | `.java`, `.kt` | `java`, `kotlin`, `groovy` |
| `javascript` | JavaScript | `.js`, `.ts`, `.jsx` | `javascript`, `typescript`, `ecmascript`, `node`, `js`, `ts` |
| `powershell` | PowerShell | `.ps1`, `.psm1` | `powershell`, `pwsh`, `ps1` |
| `batch` | Batch / CMD | `.bat`, `.cmd` | `batch`, `cmd`, `dos`, `bat` |
| `vbscript` | VBScript | `.vbs`, `.vbe`, `.wsf` | `vbscript`, `vbs`, `vb`, `vba`, `wsf` |
| `json` | JSON / YAML / INI | `.json`, `.yaml`, `.ini` | `json`, `yaml`, `yml`, `toml`, `ini`, `conf`, `cfg`, `data` |
| `bash` | Bash / Shell | `.sh`, `.bash` | `bash`, `shell`, `sh`, `zsh`, `ksh`, `console` |
| `html` | HTML / HTA | `.html`, `.hta`, `.htm` | `html`, `hta`, `xhtml`, `xml`, `markup` |
| `ruby` | Ruby | `.rb`, `.rake` | `ruby`, `rb`, `rails` |
| `sql` | SQL | `.sql`, `.sqlite` | `sql`, `sqlite`, `tsql`, `plsql` |
| `system` | System configuration | `.reg`, `.inf`, `.env`, `.conf`, `.cfg` | `registry`, `reg`, `system`, `config`, `inf`, `env`, `configuration` |

Brak dopasowania → moduł `python` (podświetlanie „po pythonowemu”), nigdy wyjątek.

**Jak to działa w praktyce:**

```python
from tabs import syntax

syntax.module_for("C++").key                  # "cpp"    (kategoria → moduł)
syntax.module_for("Registry (REG)").key       # "system" (alias w podłańcuchu)
syntax.module_for_extension("app.hta").key    # "html"   (plik → moduł)
syntax.key_for("Batch")                       # "batch"
```

---

## 4. Debugger — `tabs/debug_langs.py` (10 adapterów)

Zasada: adapter opisuje **jak język jest napisany**, nie jak jest parsowany.
Z `extensions` i `aliases` rejestr buduje mapę plik → adapter; rozstrzyganie idzie
w kolejności: *dokładny klucz → rozszerzenie → alias (najdłuższy najpierw) →
etykieta → `python`*.

| Klucz adaptera | Etykieta | Rozszerzenia | Aliasy kategorii |
|---|---|---|---|
| `python` | Python | `.py`, `.pyw`, `.pyi` | `python`, `py` |
| `javascript` | JavaScript / Node | `.js`, `.mjs`, `.cjs`, `.jsx`, `.ts`, `.tsx` | `javascript`, `node`, `typescript`, `nodejs` |
| `cpp` | C++ | `.cpp`, `.cc`, `.cxx`, `.c`, `.h`, `.hpp`, `.hxx` | `c++`, `cpp`, `c` |
| `java` | Java | `.java` | `java` |
| `powershell` | PowerShell | `.ps1`, `.psm1`, `.psd1` | `powershell`, `ps1`, `pwsh` |
| `batch` | Batch / CMD | `.bat`, `.cmd` | `batch`, `cmd`, `dos` |
| `vbscript` | VBScript | `.vbs`, `.vbe`, `.wsf` | `vbscript`, `vbs`, `vb` |
| `shell` | Shell (bash / sh) | `.sh`, `.bash`, `.zsh`, `.ksh` | `bash`, `shell`, `sh`, `zsh`, `ksh` |
| `json` | JSON | `.json`, `.jsonc`, `.json5` | `json`, `jsonc`, `json5` |
| `yaml` | YAML | `.yaml`, `.yml` | `yaml`, `yml` |

Szczególne zachowania:

- **`json` i `yaml`** to `data_format = True` — debugger ich **nie uruchamia**,
  tylko **waliduje** (`json.loads` / `yaml.safe_load`, a bez PyYAML — wbudowany
  checker wcięć i nawiasów) i raportuje liczbę kluczy/wartości.
- **`python`** ma prawdziwe sprawdzenie składni przez `ast.parse` z numerem linii
  i kolumny błędu.
- **HTML, REG, INI, Ruby i SQL w warstwie składni** nie mają adaptera debuggera —
  `adapter_for(...)` zwróci dla nich `python` jako fallback (bloki/breakpointy
  zostaną rozpoznane „po pythonowemu”, walidacja składni będzie tylko ogólna).
- **`batch` i `shell`** mają `brace_blocks = False` — głębokość bloków wynika
  z wcięć, a nie z nawiasów klamrowych.

---

## 5. Piaskownica — `tabs/sandbox_langs.py` (7 interpreterów)

Zasada: to **nie** tabela rozszerzeń, tylko tabela **języków wykonywalnych**.
Klucz wybiera `ALIASES` (kategoria skryptu → interpreter), a rozszerzenie pojawia
się jako `script_suffix` — pod jaką nazwą skrypt jest **zapisywany w workspace**
przed uruchomieniem (`_sandbox_script< suffix>`, `tabs/sandbox.py:plan_launch`).

| Klucz | Etykieta | Rozszerzenie uruchamiane | Aliasy | Poziom ograniczeń |
|---|---|---|---|---|
| `python` | Python | `.py` | `python`, `python3`, `py` | **strong** (`-I -B`) |
| `node` | Node.js | `.js` | `node`, `nodejs`, `javascript`, `js` | **moderate** |
| `java` | Java | `.java` | `java`, `jar` | **moderate** |
| `cpp` | C++ | `.cpp` | `cpp`, `c++`, `c`, `gcc`, `clang` | **weak** (kompilacja = proces potomny) |
| `powershell` | PowerShell | `.ps1` | `powershell`, `pwsh`, `ps1` | **moderate** |
| `batch` | Batch | `.bat` | `batch`, `bat`, `cmd` | **weak** |
| `vbs` | VBScript | `.vbs` | `vbs`, `vbscript` | **weak** |

Poziomy oznaczają, **ile gwarantuje sam interpreter**:

- `strong` — interpreter startuje w trybie izolowanym (bez ścieżek użytkownika,
  cache i plików startowych), a filtry in-process pokrywają jego furtki.
- `moderate` — da się wyłączyć profil i wejście interaktywne, ale nie da się
  całkowicie usunąć konfiguracji użytkownika.
- `weak` — język nie ma trybu izolacji (Batch to *zbiór poleceń hosta*);
  piaskownica broni odmową dostępu do plików, sieci i procesów potomnych,
  a nie zachowaniem interpretera.

Brak binarki (`resolve()` → `available: False`) **nie jest błędem** — to fakt
o maszynie; raport wymienia nazwy, których szukano. Aliasy pozostałych warstw
(np. `.rb`, `.html`, `.sql`, `.reg`) **nie mają interpretera** → piaskownica
użyje `python` (fallback), więc uruchomienie takiego pliku ma sens tylko jako
walidacja lub tekst.

Kolejność aliasów jest istotna: `javascript` jest aliasem `node`, a `cmd` —
aliasem `batch`, więc kategoria dobiera się poprawnie nawet bez pliku.

---

## 6. Detekcja języka bez rozszerzenia

Zasada: rozszerzenie jest **pierwszą** i mocniejszą przesłanką; jeśli plik go nie ma
albo jest nieznane, treść jest punktowana wzorcami.

- `sdb.py:detect_language_kind(code, filename, hint)` — znane rozszerzenie
  (`.cs`, `.py` …) wygrywa natychmiast; inaczej liczba trafionych wzorców
  (np. `#include <` i `std::` → C++, `@echo off` i `goto :` → Batch,
  `Option Explicit` i `WScript.` → VBScript) decyduje; przy zerze trafień
  używana jest `hint`.
- `sdb.py:guess_category(code)` — lekka podpowiedź kategorii przy zapisie:
  shebang `#!` → Bash, `<?xml` / `<!DOCTYPE html` → HTML, `<hta:` → HTA,
  `[HKEY_` → Registry, `SELECT` / `CREATE TABLE` → SQL, `Write-Host` → PowerShell,
  `console.log` → JavaScript, `System.out.println` → Java, `#include` →
  C++, poprawny JSON → JSON, `Sub` / `End Sub` → VBScript, `def` / `import` →
  Python. Brak trafienia → **brak podpowiedzi** (nie wymuszamy „Other”).

---

## 7. Skaner metadanych

`tabs/metadata_core.py:script_extensions()` **nie ma własnej tabeli prawdy**:
zbiera rozszerzenia z wszystkich adapterów `debug_langs`, a potem domyka listę
o `.py .ps1 .bat .cmd .sh .vbs .rb .java .js .ts .php .pl .sql .yaml .json`.
Dzięki temu nowy adapter dodany wtyczką jest automatycznie skanowany.
Katalogi `.git`, `__pycache__`, `.venv`, `node_modules`, `dist`, `build` są
pomijane.

---

## 8. Warstwa dokumentów (DOCs)

`tabs/docs_types.py` traktuje **skrypt jako dokument** i nadaje mu grupę `script`
lub `system`. Rozszerzenia są tu **bez kropki**, w małych literach:

| Typ dokumentu | Rozszerzenia | Grupa |
|---|---|---|
| Script / source | `py js ts java c cpp cs rb go rs sh ps1 bat cmd php pl lua` | `script` |
| SQL script | `sql` | `script` |
| Notebook | `ipynb` | `script` |
| System config | `reg service desktop plist` | `system` |

Dodatkowo nazwy bez rozszerzenia (`Makefile`, `Dockerfile`, `README`, `LICENSE`,
`CHANGELOG`, `manifest`, `todo`) rozpoznaje `FILENAME_TYPES`.

---

## 9. Macierz zbiorcza: rozszerzenie → co się dzieje

Legenda: **B** = kategoria bazy, **S** = moduł składni, **D** = adapter
debuggera, **P** = interpreter piaskownicy, **—** = brak (fallback `python`).

| Rozszerzenie | B (kategoria) | S (składnia) | D (debugger) | P (piaskownica) |
|---|---|---|---|---|
| `.py` `.pyw` `.pyi` `.pyx` | Python | python | python | python |
| `.sh` `.bash` `.zsh` `.ksh` `.shell` | Bash | bash | shell | — |
| `.ps1` `.psm1` `.psd1` | PowerShell | powershell | powershell | powershell |
| `.vbs` `.vbe` `.wsf` | VBScript | vbscript | vbscript | vbs |
| `.js` `.jsx` `.mjs` `.cjs` `.ts` `.tsx` | JavaScript | javascript | javascript | node (skrypt uruchamiany jako `.js`) |
| `.java` | Java | java | java | java |
| `.kt` | — (alias składni) | java | — | java (przez alias) |
| `.cpp` `.cc` `.cxx` `.c` `.h` `.hpp` | C++ | cpp | cpp | cpp |
| `.bat` `.cmd` `.nt` | Batch | batch | batch | batch |
| `.html` `.htm` `.xhtml` | HTML | html | — | — |
| `.hta` | HTA | html | — | — |
| `.reg` | Registry (REG) | system | — | — |
| `.json` `.jsonc` `.json5` | JSON | json | json (walidacja) | — |
| `.yaml` `.yml` | YAML | json | yaml (walidacja) | — |
| `.ini` `.toml` | INI | json | — | — |
| `.cfg` `.conf` `.inf` `.env` | System config | system | — | — |
| `.sql` `.sqlite` `.sqlite3` | SQL | sql | — | — |
| `.rb` `.rake` | Ruby | ruby | — | — |
| `.txt` / brak / nieznane | Other | python | python | python |

---

## 10. Dodawanie obsługi nowego rozszerzenia

1. **Sam import/eksport** — dodaj wpis do `EXTENSION_ALIASES` w `sdb.py`.
2. **Podświetlanie** — dodaj rozszerzenie do `extensions` odpowiedniego modułu
   w `tabs/syntax/` (albo zarejestruj nowy moduł przez `syntax.register_module`).
3. **Debugger** — analogicznie w adapterze `tabs/debug_langs.py`
   (`register_adapter` dla wtyczki).
4. **Uruchamianie** — dodaj alias do `INTERPRETERS` w `tabs/sandbox_langs.py`;
   bez tego piaskownica użyje `python` i raport powie, że brak interpretera.
5. **Skaner metadanych** — nic nie trzeba robić, jeśli krok 3 jest wykonany.

Zasada jest jedna: **rozszerzenie nigdy nie jest zgadywane z treści, jeśli jest
znane w tabeli**, a brak wpisu daje przewidywalny fallback zamiast błędu.

---

## 11. Inne skrypty uruchamialne w Windows **za pomocą Pythona**

Powyższe tabele opisują to, co aplikacja rozpoznaje **dziś**. Poniżej jest pełny
obszar tego, co można uruchomić na Windowsie *poprzez interpreter Pythona* — i co
z tego ScriptDB już obsługuje, a co mogłoby obsługiwać po rozszerzeniu tabel.

### 11.1 Trzy różne znaczenia „za pomocą Pythona”

| Model uruchomienia | Przykłady | Format pliku | Poziom zaufania |
|---|---|---|---|
| **Kod Pythona wykonywany wprost** | zwykły skrypt, `-m moduł`, `-c kod`, `pip`, `nuitka` | `.py` `.pyw` `.pyi` `.pyx` `.pyz` | pełny — kod jest wykonywany |
| **Python jako *host* dla innego języka** | `python -m http.server`, serwery WSGI/ASGI, `python -m json.tool`, `python -m pydoc` | wejście: `.wsgi` `.asgi` `.py` | Python tylko *startuje*; treść zależy od serwera aplikacji |
| **Python jako *launcher* (powłoka), a nie element** | `.lnk`, `.ps1`, `.bat`, `.vbs` / `.wsf` startujące `python.exe` | dowolny | Python nie jest wykonywany — plik tylko *wywołuje* interpreter |

Warto to rozdzielić, bo „uruchomić przez Pythona” ma trzy różne konsekwencje dla
piaskownicy: w pierwszym modelu działają wszystkie filtry in-process, w drugim —
częściowo (ochrona dotyczy procesu Pythona, nie tego, co serwer robi dalej),
a w trzecim piaskownica widzi wyłącznie launcher, a `python.exe` staje się
procesem potomnym.

### 11.2 Kategorie plików wykonywalnych/kodu, które Python obsługuje

| Kategoria | Rozszerzenia | Jak Python to uruchamia | Status w ScriptDB |
|---|---|---|---|
| **Kod źródłowy Pythona** | `.py` | `python skrypt.py` lub shebang | ✅ pełna obsługa (wszystkie 4 warstwy) |
| **Skrypt GUI bez konsoli** | `.pyw` | `pythonw.exe` (brak okna konsoli) | ✅ baza + składnia + debugger; piaskownica używa `python` |
| **Stuby typów** | `.pyi` | nie uruchamia się — to tylko podpowiedzi typów | ✅ baza + składnia + debugger (walidacja) |
| **Cython / Pyrex** | `.pyx`, `.pxd`, `.pxi` | wymaga kompilacji (`cythonize`) → moduł `.pyd` | ⚠️ `.pyx` w bazie jako Python; składnia/debugger → fallback `python` |
| **Spakowana aplikacja Python** | `.pyz` | `python archiwum.pyz` (zipapp, `__main__.py`) | ❌ nieobsługiwane |
| **Zamrożona aplikacja** | `.exe` (PyInstaller, Nuitka, cx_Freeze) | uruchamiasz `.exe`, Python jest *wewnątrz* | ❌ i **nie powinno się** uruchamiać w piaskownicy — nie da się jej *czytać* jako kodu |
| **Konfiguracja narzędzi Pythona** | `.pth` (ścieżki `site`), `.cfg`/`.toml` (setuptools, pyproject), `requirements.txt`, `Pipfile` | nie uruchamiane bezpośrednio, czytane przez `site`/`pip` | ⚠️ `.cfg`/`.toml` → `System config`/`INI`; reszta jako `Other` / źródła struktury projektu |
| **Notatnik** | `.ipynb` | `jupyter nbconvert --execute` | ⚠️ DOCs zna go jako `notebook`; nie ma go w `CATEGORIES` |
| **Testy i narzędzia** | pliki `test_*.py`, `conftest.py`, `manage.py` | `python -m pytest`, `django-admin` | ➖ zwykłe `.py` — rozpoznawane po nazwie, nie po rozszerzeniu |
| **Moduł z CLI** | `.py` + `__main__.py` w katalogu | `python -m pakiet` | ➖ to katalog, nie rozszerzenie |

### 11.3 Format pośredni: Python jako host, nie jako treść

| Format | Rozszerzenia | Co robi Python | Uwaga dla piaskownicy |
|---|---|---|---|
| **WSGI / ASGI** | `.wsgi`, `.asgi` | aplikacja ładowana przez serwer (mod_wsgi, gunicorn, uvicorn) | Python importuje moduł, ale punkt wejścia jest *plikiem konfiguracyjnym* serwera |
| **Rozszerzenia natywne** | `.pyd`, `.dll` | import binarny — **nie** kodu źródłowego | piaskownica nie powinna ich ładować; to kod maszynowy |
| **Archiwum Pythona** | `.pyz`, `.zip` | `zipapp` / import z zipa | po rozpakowaniu zwykłe `.py` |

### 11.4 Launchery Windows, które uruchamiają Pythona

Te pliki **nie są Pythonem**, ale `python.exe` jest w nich najczęstszym celem —
dlatego warto je znać przy planowaniu „Uruchom przez Python”:

| Launcher | Rozszerzenia | Status w ScriptDB | Uruchomienie przez Python? |
|---|---|---|---|
| **PowerShell** | `.ps1` | ✅ wszystkie warstwy | tak, jeśli treść wywołuje `python` — piaskownica uruchomi **PowerShell**, nie Python |
| **Batch / CMD** | `.bat`, `.cmd` | ✅ wszystkie warstwy | tak (`python skrypt.py` w środku) |
| **VBScript / WSH** | `.vbs`, `.vbe`, `.wsf` | ✅ wszystkie warstwy | tak, przez `CreateObject("WScript.Shell").Run "python ..."` |
| **JScript (Windows Script Host)** | `.js`, `.jse` | ✅ `.js` jako JavaScript; `.jse` ❌ | tak, WSH uruchamia JScript (`cscript`), nie Pythona |
| **HTA (aplikacja HTML)** | `.hta` | ✅ kategoria + składnia HTML | tylko jeśli strona wywołuje Python (np. przez WSH) |
| **Skrót** | `.lnk` | ❌ | **nie da się** — to binarny plik powłoki, nie kod |
| **Plik komend** | `.cmd`/`.bat` + `python.exe` | ✅ jako Batch | tak |
| **Rejestr** | `.reg` | ✅ kategoria + `system` | nie — `reg` importuje wpisy, nie kod |
| **INF** | `.inf` | ✅ przez `system` | nie |
| **File Association / Shell Extension** | `.com`, `.scr` | ❌ | nie — to binaria lub ustawienia systemowe |

### 11.5 Co realnie warto dodać do tabel (i gdzie)

Poniższe wpisy **nie zmieniają kodu** — to propozycja, jak rozszerzyć obsługę,
gdyby była potrzebna. Każdy wpis pokazuje dokładnie tę warstwę, której dotyczy.

| Propozycja | Gdzie | Efekt |
|---|---|---|
| `.pyz` | `sdb.py:EXTENSION_ALIASES` → `"Python"`; `tabs/debug_langs.py` → `PythonAdapter.extensions` | rozpoznanie zipapp jako Pythona (do odczytu), walidacja niemożliwa — to zip |
| `.pxd`, `.pxi` | `sdb.py:EXTENSION_ALIASES` → `"Python"`; składnia `python_lang.extensions` | deklaracje Cythona podświetlą się jako Python |
| `.jse` | `sdb.py:EXTENSION_ALIASES` → `"JavaScript"`; składnia `javascript_lang.extensions` | JScript WSH podświetli się jako JavaScript |
| `.asgi`, `.wsgi` | nowa kategoria / `Other` + adnotacja w `sandbox_langs` | uruchamiane tylko przez serwer aplikacji — piaskownica powinna je *odrzucać*, nie uruchamiać jako `.py` |
| `.ipynb` | `sdb.py:CATEGORIES` + `EXTENSION_ALIASES` → `"JSON"` | notatnik otworzy się jako JSON; już jest znany w `docs_types` |
| `.pyd`, `.dll`, `.exe`, `.com`, `.scr`, `.lnk` | **nigdzie** — celowo | to binaria: piaskownica nie może ich analizować jako kodu, a „uruchom przez Python” jest dla nich niemożliwe |

### 11.6 Zasada bezpieczeństwa dla tej grupy

„Uruchom przez Python” **nie oznacza**, że piaskownica wykona Python. Wynika to
wprost z `tabs/sandbox_langs.py`: interpreter wybiera **język skryptu**, nie
zawartość pliku. Dlatego:

- `.py` → Python (`strong`, flagi `-I -B`),
- `.pyw`, `.pyi`, `.pyx`, `.pyz` → jeśli nie mają własnego wpisu, trafią do
  `python` przez fallback,
- `.ps1`, `.bat`, `.vbs` → uruchamia **ich własny interpreter** (`moderate` / `weak`),
  nawet jeśli w środku jest wywołanie `python.exe`; Python będzie wtedy
  *procesem potomnym*, który piaskownica filtruje **w tym samym zakresie** co
  proces nadrzędny (patrz: `plan_launch` i hosting języków `weak`),
- `.exe`, `.pyd`, `.dll` → nie są kodem źródłowym — piaskownica ich nie uruchamia
  i nie analizuje.

Dlatego „inne skrypty, które można uruchomić w Windows za pomocą Pythona” dzielą się
na **trzy klasy**: (1) uruchamiane bezpośrednio jako Python, (2) uruchamiane przez
Pythona jako host (WSGI/ASGI/zipapp), (3) uruchamiane przez **inny** launcher,
który jedynie *wywołuje* Pythona. Tylko pierwsza klasa daje piaskownicy te same
gwarancje co `.py`.

---

## 12. Podsumowanie rozszerzeń według klasy uruchamiania

| Rozszerzenie | Klasa | Czy piaskownica to uruchomi? |
|---|---|---|
| `.py` | kod Pythona | ✅ `python` (`strong`) |
| `.pyw` | kod Pythona (GUI) | ✅ `python` (fallback; brak trybu okienkowego) |
| `.pyi` | stub — nieuruchamialny | ➖ walidacja / podświetlanie |
| `.pyx`, `.pxd`, `.pxi` | kod do kompilacji | ➖ walidacja / podświetlanie (po dodaniu wpisu) |
| `.pyz` | archiwum Pythona | ⚠️ dopiero po dodaniu wpisu — jako `.py` piaskownica go **nie** odczyta |
| `.wsgi`, `.asgi` | kod hostowany przez serwer | ❌ piaskownica nie ma serwera aplikacji |
| `.ipynb` | notatnik (JSON) | ❌ tylko podgląd jako JSON |
| `.ps1` `.bat` `.cmd` `.vbs` `.wsf` `.js` | launcher wywołujący Pythona | ✅ ale **jako ten launcher**, nie jako Python |
| `.exe` `.pyd` `.dll` `.lnk` `.com` `.scr` | binaria / powłoka | ❌ celowo |