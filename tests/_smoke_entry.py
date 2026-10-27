"""Temporary: run the real entry point, quit after a moment, and exit."""
import sys
import time
import os
import subprocess

def install_dependencies():
    """Spróbuj zainstalować wymagane zależności automatycznie"""
    print("\n=== Próba instalacji wymaganych zależności... ===")
    try:
        # Zainstaluj PyQt5
        subprocess.check_call([sys.executable, "-m", "pip", "install", "PyQt5"])
        print("✓ PyQt5 zainstalowane pomyślnie!")
        return True
    except subprocess.CalledProcessError:
        print("✗ Nie udało się zainstalować PyQt5 automatycznie.")
        print("\nAby zainstalować ręcznie, wpisz w terminalu:")
        print("pip install PyQt5")
        return False

def main():
    # Dodaj katalog projektu do ścieżki, aby można było zaimportować sdb_scripts
    project_dir = os.path.dirname(os.path.abspath(__file__))
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    
    print("=== Smoke test ScriptDB - uruchamianie głównego pliku wejściowego ===")
    
    def try_run_main():
        """Pomocnicza funkcja do próby uruchomienia głównego skryptu"""
        try:
            import sdb
            return True, None
        except SystemExit as e:
            return False, e
        except Exception as e:
            return False, e

    # Pierwsza próba uruchomienia
    success, error = try_run_main()

    if success:
        print("\n✓ Pomyślnie zaimportowano główny plik wejściowy!")
        print("Program będzie zamknięty za 2 sekundy...")
        time.sleep(2)
        print("\n✓ Smoke test zakończony powodzeniem!")
        sys.exit(0)
    else:
        # Wystąpił błąd - najprawdopodobniej brakuje zależności
        print(f"\nGłówny plik wejściowy nie mógł się uruchomić: {error}")
        print("Najprawdopodobniej brakuje wymaganych bibliotek.")

        # Zaproponuj instalację zależności
        if install_dependencies():
            # Jeśli instalacja się powiodła, spróbuj ponownie zaimportować
            print("\n=== Ponowna próba uruchomienia programu... ===")
            try:
                import importlib
                # Wymuś ponowne załadowanie modułu
                if 'sdb' in sys.modules:
                    del sys.modules['sdb']
                import sdb
                print("\n✓ Program uruchomiony pomyślnie po instalacji zależności!")
                time.sleep(2)
                sys.exit(0)
            except Exception as reload_error:
                print(f"\nNawet po instalacji wystąpił błąd: {reload_error}")
                sys.exit(1)
        sys.exit(1)

if __name__ == "__main__":
    main()