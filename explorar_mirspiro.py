"""
Explorador de UI de MirSpiro (uiautomation).
Lanza la app, espera carga y vuelca el árbol de controles.
"""

import sys
import subprocess
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
import uiautomation as uia


def explorer():
    exe = config.MIRSPIRO_EXE
    if not exe:
        print("ERROR: MIRSPIRO_EXE no está configurado en .env")
        sys.exit(1)

    print(f"Iniciando MirSpiro desde: {exe}")
    subprocess.Popen(exe)

    print("Esperando ventana principal (hasta 30s)…")
    main_window = None
    deadline = time.monotonic() + 30

    while time.monotonic() < deadline:
        for w in uia.GetRootControl().GetChildren():
            if w.ControlTypeName == "WindowControl" and "Mir Spiro" in (w.Name or ""):
                main_window = w
                break
        if main_window:
            break
        time.sleep(0.5)

    if main_window is None:
        print("No se detectó ninguna ventana")
        sys.exit(1)

    print(f"\nVentana activa: '{main_window.Name}'")
    print(f"ClassName: {main_window.ClassName}")
    print(f"ControlType: {main_window.ControlTypeName}")
    print(f"AutomationId: {main_window.AutomationId}")
    print("=" * 70)

    # Volcado del árbol
    def print_tree(ctrl, depth=0, max_depth=5):
        if depth > max_depth:
            return
        indent = "  " * depth
        name = (ctrl.Name or "")[:50]
        print(f"{indent}[{ctrl.ControlTypeName}] Name='{name}' Class='{ctrl.ClassName}' AutoId='{ctrl.AutomationId}'")
        for child in ctrl.GetChildren():
            print_tree(child, depth + 1, max_depth)

    print_tree(main_window)

    print("\n" + "=" * 70)
    print("Presiona Enter para cerrar la app…")
    input()
    try:
        main_window.GetWindowPattern().Close()
    except Exception:
        pass


if __name__ == "__main__":
    explorer()
