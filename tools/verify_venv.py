import sys
import subprocess
import platform
from pathlib import Path


def run_and_print(command: list[str]) -> int:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    return result.returncode


def check_dotenv(python_path: Path, project_root: Path) -> bool:
    env_path = project_root / ".env"
    if env_path.exists():
        print(f"\n.env file found at: {env_path}")
    else:
        print(f"\n.env file not found at: {env_path}")
        return False

    check_script = (
        "from pathlib import Path; "
        "from dotenv import dotenv_values, load_dotenv; "
        f"env_path = Path({str(env_path)!r}); "
        "loaded = load_dotenv(env_path); "
        "values = dotenv_values(env_path); "
        "print('python-dotenv import: ok'); "
        "print(f'.env load result: {loaded}'); "
        "print(f'.env keys loaded: {len(values)}'); "
        "raise SystemExit(0 if values else 1)"
    )

    returncode = run_and_print([str(python_path), "-c", check_script])
    if returncode == 0:
        print(".env loaded successfully")
        return True

    print("python-dotenv check failed or .env did not contain any keys")
    return False


def main():
    project_root = Path(__file__).resolve().parents[1]
    venv_dir = project_root / ".venv"

    print(f"Checking .venv at: {venv_dir}")

    if venv_dir.exists():
        print (".venv directory found under project root")
    else:
        print(".venv directory does not exist under project root")
        return

    cfg_path = venv_dir / "pyvenv.cfg"
    if cfg_path.exists():
        print("pyvenv.cfg found under .venv")
    else:
        print("pyvenv.cfg not found - Virtual environment not valid")
        return
    
    # resolve interpreter path for current OS
    if platform.system() == "Windows":
        python_path = venv_dir / "Scripts" / "python.exe"
    else:
        python_path = venv_dir / "bin" / "python"
    
    if python_path.exists():
        print(f"\nExpected python interpreter found at: {python_path}")
    else:
        print(f"\nExpected python interpreter not found at: {python_path}")
        return
    
    print("\nPython executable info:")
    run_and_print([str(python_path), "--version"])
    
    print("\nPip executable and version:")
    run_and_print([str(python_path), "-m", "pip", "--version"])

    if not check_dotenv(python_path, project_root):
        return
    
    print("\n.venv looks healthy")
    sys.exit(0)

if __name__ == "__main__":
    main()
