"""
Explorador de UI de MirSpiro.
Lanza la app, espera carga y vuelca el árbol de controles
para poder ajustar los selectores del módulo.
"""

import sys
from pathlib import Path

# Agregar raíz del proyecto al path
sys.path.insert(0, str(Path(__file__).parent))

import config
from pywinauto import Application
from pywinauto.timings import wait_until_passes


def explorer():
    exe = config.MIRSPIRO_EXE
    if not exe:
        print("ERROR: MIRSPIRO_EXE no está configurado en .env")
        sys.exit(1)

    print(f"Iniciando MirSpiro desde: {exe}")
    app = Application(backend="uia").start(exe, timeout=60)

    print("Esperando ventana principal (hasta 30s)…")
    # Intentar varios title_re conocidos
    posibles = ["MIR Spiro", "MIR", "MirSpiro"]
    main_window = None
    for title in posibles:
        try:
            main_window = app.window(title_re=f".*{title}.*")
            main_window.wait("visible", timeout=5)
            break
        except Exception:
            continue

    if main_window is None:
        # Tomar la primera ventana top-level visible
        main_window = app.top_window()

    print(f"\nVentana activa: '{main_window.window_text()}'")
    print(f"Clase: {main_window.class_name()}")
    print(f"Control type: {main_window.element_info.control_type}")
    print("=" * 70)

    # Volcado completo del árbol UI
    main_window.print_control_identifiers(depth=None)

    print("\n" + "=" * 70)
    print("Para cerrar la app, presiona Enter…")
    input()
    app.kill()


if __name__ == "__main__":
    explorer()
