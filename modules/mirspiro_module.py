"""
MirSpiro Automation Module
==========================
Automatización RPA de escritorio para MirSpiro ("Mir Spiro 2.1.11").

ESTRATEGIA:
  - uiautomation (UI Automation via COM, sin dependencia de pywin32)
  - Funciona en Python 3.12+ / 3.14+
  - Selectores configurables vía dict DEFAULT_SELECTORS
  - Esperas por existencia/habilitación, retry con backoff
  - Logging detallado por paso
  - UIA como mecanismo principal; pyautogui solo como último recurso
    para el diálogo nativo "Guardar como" de Windows (pendiente de
    confirmación mediante exploración).

FLUJO REAL CONFIRMADO POR VOLCADO UIA:
  1. Abrir MirSpiro
  2. Esperar 10s (timer interno de la app), localizar y hacer clic en
     el Banner de versión gratuita (AutoId='Banner' → TextBlock
     "¡Haga clic aquí!") para cerrarlo.
  3. Escribir cédula en input "Buscar pacientes" (AutoId='ptbSearch'),
     limpiar primero vía ClearButton (AutoId='ClearButton', hijo de
     ptbSearch).
  4. Click "Imprimir" (AutoId='printButton', dentro de
     TestMenuUserControl) → se abre modal de impresión como
     WindowControl hija de la ventana principal.
  5. Click "Guardar PDF" (AutoId='savePdfBtn', dentro del modal).
  6. Diálogo "Guardar como" de Windows (no capturado en volcado aún
     — pendiente de exploración).
  7. Click "Cancelar" en modal de impresión (DesertImagelessButton
     cuyo TextBlock hijo tiene Name='Cancelar').
  8. Siguiente paciente (misma ventana abierta).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import uiautomation as uia

log = logging.getLogger("bot_espirometrias.mirspiro")

# ──────────────────────────────────────────────
# Selectores UI por defecto (confirmados por volcado UIA real)
# ──────────────────────────────────────────────
DEFAULT_SELECTORS: dict[str, Any] = {
    "main_window_title": "Mir Spiro",
    "banner_auto_id": "Banner",
    "banner_dismiss_text": "¡Haga clic aquí!",
    "startup_wait": 10,
    "continue_button_auto_id": "BtnContinue",
    "search_field_auto_id": "ptbSearch",
    "clear_button_auto_id": "ClearButton",
    "print_button_auto_id": "printButton",
    "save_pdf_auto_id": "savePdfBtn",
    "cancel_button_child_name": "Cancelar",
    "no_results_texts": [
        "No se encontraron resultados",
        "No se encontró correspondencia",
    ],
    "message_box_auto_id": "messageBox",
    "message_box_confirm_auto_id": "leftButton",
    "message_box_cancel_auto_id": "rightButton",
    "message_box_close_auto_id": "CloseButton",
    "exporting_type_combo_auto_id": "ExportingTypeComboBox",
}


def _build_pdf_filename(cedula: str) -> str:
    return f"{cedula}.pdf"


def _dump_uia_tree(control: uia.Control, max_depth: int = 6) -> str:
    """Retorna un dump texto del árbol UIA a partir de control."""
    lines: list[str] = []

    def walk(ctrl, depth=0):
        if depth > max_depth:
            return
        indent = "  " * depth
        name = (ctrl.Name or "")[:60]
        lines.append(
            f"{indent}[{ctrl.ControlTypeName}] Name='{name}' "
            f"Class='{ctrl.ClassName}' AutoId='{ctrl.AutomationId}'"
        )
        try:
            for child in ctrl.GetChildren():
                walk(child, depth + 1)
        except Exception:
            pass

    walk(control)
    return "\n".join(lines)


class MirSpiroAutomation:
    def __init__(
        self,
        sede: str,
        output_dir: str,
        executable_path: str | None = None,
        selectors: dict | None = None,
        typing_delay: float = 0.05,
        retry_attempts: int = 2,
    ) -> None:
        self.sede = sede
        self.output_dir = Path(output_dir)
        self.executable_path = executable_path
        self.selectors = {**DEFAULT_SELECTORS, **(selectors or {})}
        self.typing_delay = typing_delay
        self.retry_attempts = retry_attempts
        self.main_window: uia.WindowControl | None = None
        self._debug_dir = Path(__file__).parent.parent / "debug"
        self._debug_dir.mkdir(parents=True, exist_ok=True)

        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── helpers ────────────────────────────────────────────

    def _diagnostic(self, tag: str) -> None:
        """Guarda screenshot + dump UIA en debug/ ante fallos."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{ts}_{tag}"
        try:
            import pyautogui
            ss_path = self._debug_dir / f"{name}.png"
            pyautogui.screenshot(str(ss_path))
            log.info("Screenshot guardado: %s", ss_path)
        except Exception:
            pass
        try:
            tree_path = self._debug_dir / f"{name}.txt"
            root = uia.GetRootControl()
            tree = _dump_uia_tree(root, max_depth=8)
            tree_path.write_text(tree, encoding="utf-8")
            log.info("Árbol UIA guardado: %s", tree_path)
        except Exception:
            pass

    def _retry(self, fn, *args, **kwargs):
        last_err = None
        for attempt in range(1, self.retry_attempts + 2):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_err = e
                log.warning("Intento %d/%d falló: %s", attempt, self.retry_attempts + 1, e)
                if attempt <= self.retry_attempts:
                    time.sleep(attempt * 1.0)
        self._diagnostic("retry_exhausted")
        raise last_err  # type: ignore[misc]

    def _find_control(
        self,
        parent: uia.Control,
        condition: dict,
        timeout: float = 5,
    ) -> uia.Control:
        """Busca un control hijo recursivamente por criterios con timeout."""
        control_type = condition.get("control_type", "")
        name = condition.get("name")
        auto_id = condition.get("auto_id")
        class_name = condition.get("class_name")

        def _match(c: uia.Control) -> bool:
            if auto_id and c.AutomationId != auto_id:
                return False
            if name and c.Name != name:
                return False
            if class_name and c.ClassName != class_name:
                return False
            if control_type and c.ControlTypeName != control_type:
                return False
            return bool(auto_id or name or class_name or control_type)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for child in self._collect_children(parent):
                if _match(child):
                    return child
            time.sleep(0.3)

        raise RuntimeError(f"Control no encontrado: {condition}")

    def _find_control_with_child(
        self,
        parent: uia.Control,
        child_criteria: dict,
        timeout: float = 5,
    ) -> uia.Control:
        """Busca un control (recursivamente) que tenga un hijo que cumpla child_criteria."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for candidate in self._collect_children(parent):
                try:
                    for child in candidate.GetChildren():
                        match = True
                        if "name" in child_criteria:
                            if child.Name != child_criteria["name"]:
                                match = False
                        if "auto_id" in child_criteria:
                            if child.AutomationId != child_criteria["auto_id"]:
                                match = False
                        if "control_type" in child_criteria:
                            if child.ControlTypeName != child_criteria["control_type"]:
                                match = False
                        if match:
                            return candidate
                except Exception:
                    continue
            time.sleep(0.3)
        raise RuntimeError(f"Control con hijo no encontrado: {child_criteria}")

    def _find_control_anywhere(
        self,
        condition: dict,
        timeout: float = 5,
    ) -> uia.Control:
        """Busca el control primero en main_window, luego en todas las ventanas top-level."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return self._find_control(self.main_window, condition, timeout=1)
            except RuntimeError:
                pass
            try:
                root = uia.GetRootControl()
                for w in root.GetChildren():
                    if w.ControlTypeName == "WindowControl":
                        try:
                            return self._find_control(w, condition, timeout=1)
                        except RuntimeError:
                            continue
            except Exception:
                pass
            time.sleep(0.3)
        raise RuntimeError(f"Control no encontrado en ningún nivel: {condition}")

    @staticmethod
    def _collect_children(parent, max_depth=10, _depth=0, _results=None):
        """Recolecta todos los controles hijos recursivamente."""
        if _results is None:
            _results = []
        if _depth >= max_depth:
            return _results
        for child in parent.GetChildren():
            _results.append(child)
            MirSpiroAutomation._collect_children(child, max_depth, _depth + 1, _results)
        return _results

    # ── 1. Conexión / apertura ────────────────────────────

    def conectar(self, timeout: float = 30) -> None:
        """Inicia MirSpiro, cierra el modal de suscripción y localiza la ventana principal."""
        if self.executable_path:
            log.info("Iniciando MirSpiro desde %s", self.executable_path)
            import subprocess
            subprocess.Popen(self.executable_path)
        else:
            log.info("Conectando a MirSpiro ya en ejecución…")

        # ── Localizar ventana principal primero ──
        title_re = self.selectors["main_window_title"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for w in uia.GetRootControl().GetChildren():
                if w.ControlTypeName == "WindowControl" and title_re in (w.Name or ""):
                    self.main_window = w
                    break
            if self.main_window:
                break
            time.sleep(0.5)

        if not self.main_window:
            raise RuntimeError(f"No se encontró ventana con título '{title_re}'")
        log.info("Ventana principal: '%s'", self.main_window.Name)

        # ── Cerrar Banner de versión gratuita (UIA) ──
        log.info("Cerrando banner de versión gratuita…")
        self._cerrar_startup_modal()
        log.info("Banner procesado, continuando…")

    # ── 1a. Cierre del Banner de versión gratuita ────────

    def _cerrar_startup_modal(self) -> None:
        """
        Cierra el modal de suscripción inicial usando UIA.

        La app renderiza el modal como una WindowControl hija de la
        ventana principal, con un botón DesertImagelessButton
        AutoId='BtnContinue' (TextBlock hijo 'Continuar').
        El timer interno de ~10s impide interactuar antes.

        Busca en TODAS las ventanas top-level como respaldo si el modal
        no aparece como hijo de main_window.
        """
        sel = self.selectors
        wait_time = sel.get("startup_wait", 10)
        log.info("Esperando %ds a que el modal sea interactuable…", wait_time)
        time.sleep(wait_time)

        # Buscar en main_window y root simultáneamente (el modal puede estar en cualquier nivel)
        try:
            btn = self._find_control_anywhere(
                {"auto_id": sel["continue_button_auto_id"]},
                timeout=10,
            )
            log.info("Click en 'Continuar' (BtnContinue)")
            btn.Click()
            time.sleep(1)
            log.info("Modal de inicio cerrado")
            return
        except RuntimeError:
            log.info("BtnContinue no encontrado por UIA, usando fallback…")

        # Último recurso: pyautogui sobre coordenadas relativas
        import pyautogui

        rect = self.main_window.BoundingRectangle
        if rect:
            win_w = rect.right - rect.left
            win_h = rect.bottom - rect.top
            cx = rect.left + int(win_w * 0.645)
            cy = rect.top + int(win_h * 0.88)
            log.info("Fallback: click en 'Continuar' en (%d, %d)", cx, cy)
            pyautogui.click(cx, cy)
            time.sleep(1)
            log.info("Modal cerrado (fallback coordenadas)")
        else:
            log.warning("No se pudieron obtener bounds, no se cerró el modal")

    # ── 2. Búsqueda de paciente ───────────────────────────

    def buscar_paciente(self, cedula: str) -> None:
        """Escribe la cédula en el campo 'Buscar pacientes' y presiona Enter.

        Limpia el campo usando el ClearButton (AutoId='ClearButton', hijo
        de ptbSearch) en vez de coordenadas fijas.
        """
        sel = self.selectors
        log.info("Buscando paciente %s…", cedula)

        # 1. Traer MirSpiro al frente y enfocar el campo de búsqueda
        import pyautogui
        try:
            self.main_window.SetFocus()
            time.sleep(0.2)
        except Exception:
            pass
        search = self._retry(self._find_search_field, sel)
        try:
            search.SetFocus()
        except Exception:
            search.Click()
        time.sleep(0.2)

        # 2. Limpiar vía ClearButton (UIA)
        try:
            clear_btn = self._find_control(
                search,
                {"auto_id": sel["clear_button_auto_id"]},
                timeout=2,
            )
            log.debug("Click en ClearButton")
            clear_btn.Click()
            time.sleep(0.1)
        except RuntimeError:
            log.debug("ClearButton no encontrado, campo posiblemente vacío")

        # 3. Doble limpieza por teclado (por si ClearButton falla o el foco se perdió)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.05)
        pyautogui.press("delete")
        time.sleep(0.1)

        # 4. Enviar cédula carácter por carácter (MirSpiro necesita pausa entre teclas)
        for ch in cedula:
            search.SendKeys(ch, waitTime=self.typing_delay)

        search.SendKeys("{ENTER}")
        time.sleep(0.3)
        log.info("Búsqueda ejecutada para %s", cedula)

    def _find_search_field(self, sel: dict) -> uia.Control:
        aid = sel.get("search_field_auto_id")
        if aid:
            return self._find_control(self.main_window, {"auto_id": aid})
        return self._find_control(self.main_window, {"control_type": "EditControl"})

    # ── 3. Imprimir → Guardar PDF ─────────────────────────

    def exportar_pdf(self, cedula: str) -> str:
        sel = self.selectors
        pdf_name = _build_pdf_filename(cedula)
        pdf_path = self.output_dir / pdf_name
        log.info("Exportando PDF: %s", pdf_path)

        self._click_imprimir(sel)
        self._esperar_modal_impresion()
        self._click_guardar_pdf()
        self._guardar_como(pdf_path, sel)
        self._cerrar_modal_impresion()

        if not pdf_path.exists():
            raise RuntimeError(f"No se generó el PDF: {pdf_path}")
        if pdf_path.stat().st_size == 0:
            pdf_path.unlink(missing_ok=True)
            raise RuntimeError(f"PDF vacío: {pdf_path}")

        log.info("PDF OK: %s (%d bytes)", pdf_path, pdf_path.stat().st_size)
        return str(pdf_path.resolve())

    def _click_imprimir(self, sel: dict) -> None:
        """Click en el botón 'Imprimir' (AutoId='printButton') vía UIA."""
        btn = self._retry(
            self._find_control,
            self.main_window,
            {"auto_id": sel["print_button_auto_id"]},
        )
        btn.Click()
        log.info("Click en 'Imprimir' (printButton)")
        time.sleep(1)

    def _esperar_modal_impresion(self) -> None:
        """Espera a que el modal de impresión esté presente (savePdfBtn visible)."""
        log.info("Esperando modal de impresión…")
        self._find_control(
            self.main_window,
            {"auto_id": self.selectors["save_pdf_auto_id"]},
            timeout=8,
        )
        log.debug("Modal de impresión detectado (savePdfBtn visible)")

    def _click_guardar_pdf(self) -> None:
        """Click en 'Guardar PDF' (AutoId='savePdfBtn') dentro del modal de impresión vía UIA."""
        btn = self._find_control(
            self.main_window,
            {"auto_id": self.selectors["save_pdf_auto_id"]},
            timeout=5,
        )
        btn.Click()
        log.info("Click en 'Guardar PDF' (savePdfBtn)")

        # Verificación post-clic: esperar que el diálogo nativo aparezca
        # (se confirma en _guardar_como)
        time.sleep(1)

    def _guardar_como(self, pdf_path: Path, sel: dict) -> None:
        """
        Maneja el diálogo "Guardar como" de Windows.

        1. Elimina PDF existente para evitar diálogo de sobrescritura.
        2. Espera a que aparezca el diálogo (busca en main_window y root).
        3. Trae el diálogo al frente antes de escribir.
        4. Escribe la ruta completa vía pyautogui (el EditControl del nombre
           de archivo está muy anidado dentro del diálogo nativo de Windows).
        5. Hace clic en "Guardar" (ButtonControl AutoId='1') vía UIA.
        6. Si aparece confirmación de sobrescritura, la acepta automáticamente.
        """
        import pyautogui
        import ctypes

        # Eliminar PDF existente para evitar diálogo de sobrescritura
        if pdf_path.exists():
            pdf_path.unlink()
            log.debug("PDF existente eliminado: %s", pdf_path)

        log.info("Esperando diálogo Guardar como…")

        # Buscar el diálogo en main_window y root (es una ventana top-level)
        dlg = self._find_control_anywhere(
            {"name": "Guardar como", "control_type": "WindowControl"},
            timeout=10,
        )
        log.debug("Diálogo Guardar como detectado")

        # Traer el diálogo al frente usando la HWND nativa
        try:
            handle = dlg.NativeWindowHandle
            if handle:
                ctypes.windll.user32.SetForegroundWindow(handle)
                time.sleep(0.3)
        except Exception:
            pass

        time.sleep(0.5)

        # Escribir ruta (pyautogui — el EditControl del nombre está dentro
        # de DUIViewWndClassName → HWNDView → ... difícil de localizar)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.typewrite(str(pdf_path.resolve()), interval=0.02)
        time.sleep(0.3)

        # Click "Guardar" vía UIA (AutoId='1'), buscar en root como fallback
        try:
            guardar_btn = self._find_control_anywhere(
                {"auto_id": "1", "control_type": "ButtonControl"},
                timeout=3,
            )
            guardar_btn.Click()
            log.debug("Click en Guardar vía UIA")
        except RuntimeError:
            log.warning("Botón Guardar no encontrado por UIA, fallback a Enter")
            pyautogui.press("enter")

        time.sleep(1)

        # Si aparece diálogo de confirmación de sobrescritura, aceptarlo
        try:
            confirm = self._find_control_anywhere(
                {"name": "Confirmar guardar como", "control_type": "WindowControl"},
                timeout=2,
            )
            log.info("Diálogo de sobrescritura detectado, aceptando…")
            si_btn = self._find_control_anywhere(
                {"auto_id": "6", "control_type": "ButtonControl"},
                timeout=2,
            )
            si_btn.Click()
            time.sleep(1)
        except RuntimeError:
            pass

    def _cerrar_modal_impresion(self) -> None:
        """
        Cierra el modal de impresión haciendo clic en 'Cancelar' vía UIA.

        Localiza el DesertImagelessButton cuyo TextBlock hijo tiene
        Name='Cancelar', dentro del modal. Verifica que el modal
        desaparezca del árbol tras el clic.
        """
        sel = self.selectors

        cancel_btn = self._find_control_with_child(
            self.main_window,
            {"name": sel["cancel_button_child_name"]},
            timeout=5,
        )
        log.info("Click en 'Cancelar' del modal de impresión")
        cancel_btn.Click()
        time.sleep(1)

        # Verificar que savePdfBtn ya no esté visible (modal cerrado)
        try:
            self._find_control(
                self.main_window,
                {"auto_id": sel["save_pdf_auto_id"]},
                timeout=3,
            )
            raise RuntimeError(
                "El modal de impresión sigue presente tras hacer clic en Cancelar"
            )
        except RuntimeError as e:
            if "sigue presente" in str(e):
                self._diagnostic("modal_no_cerrado")
                raise
            pass

        log.info("Modal de impresión cerrado correctamente")

    # ── 4. Mensajes y diálogos internos (MirMessageBox) ────

    def _confirmar_message_box(self) -> None:
        """Hace clic en CONFIRMAR (leftButton) del MirMessageBox global."""
        sel = self.selectors
        msgbox = self._find_control(
            self.main_window,
            {"auto_id": sel["message_box_auto_id"]},
            timeout=3,
        )
        confirm = self._find_control(
            msgbox,
            {"auto_id": sel["message_box_confirm_auto_id"]},
            timeout=2,
        )
        confirm.Click()
        log.debug("MirMessageBox: clic en CONFIRMAR")

    def _cancelar_message_box(self) -> None:
        """Hace clic en CANCELAR (rightButton) del MirMessageBox global."""
        sel = self.selectors
        msgbox = self._find_control(
            self.main_window,
            {"auto_id": sel["message_box_auto_id"]},
            timeout=3,
        )
        cancel = self._find_control(
            msgbox,
            {"auto_id": sel["message_box_cancel_auto_id"]},
            timeout=2,
        )
        cancel.Click()
        log.debug("MirMessageBox: clic en CANCELAR")

    # ── 5. Verificaciones ─────────────────────────────────

    def _paciente_encontrado(self) -> bool:
        """
        Verifica si la búsqueda encontró un paciente.

        Estrategias (cualquiera indica éxito):
        A. El botón printButton está habilitado.
        B. NO aparece ninguno de los textos de "sin resultados"
           Y el search field está vacío (indicando que se limpió tras búsqueda exitosa).
        """
        sel = self.selectors

        # Estrategia A: printButton habilitado
        try:
            btn = self._find_control(
                self.main_window,
                {"auto_id": sel["print_button_auto_id"]},
                timeout=3,
            )
            if btn.IsEnabled:
                return True
        except Exception:
            pass

        # Estrategia B: verificar que NO haya textos de "sin resultados"
        #               y que el campo de búsqueda tenga contenido
        try:
            for no_result_text in sel.get("no_results_texts", []):
                try:
                    self._find_control(
                        self.main_window,
                        {"name": no_result_text},
                        timeout=1,
                    )
                    return False
                except RuntimeError:
                    continue

            # Confirmación adicional: el search field debería tener texto
            try:
                search = self._find_search_field(sel)
                if search.Name or search.GetValuePattern().Value:
                    return True
            except Exception:
                pass

            return False
        except Exception:
            return False

    # ── 6. Flujo completo ─────────────────────────────────

    def procesar_paciente(self, cedula: str) -> dict:
        """Busca paciente, exporta PDF y cierra modal. Requiere conectar() antes."""
        result: dict = {"success": False, "pdf_path": None, "error": None}
        try:
            self.buscar_paciente(cedula)
            time.sleep(0.5)
            if not self._paciente_encontrado():
                raise RuntimeError("Paciente no encontrado en MirSpiro")
            pdf_path = self.exportar_pdf(cedula)
            result["success"] = True
            result["pdf_path"] = pdf_path
        except Exception as e:
            log.exception("Error con paciente %s: %s", cedula, e)
            self._diagnostic(f"error_{cedula}")
            result["error"] = str(e)
        return result

    def limpiar_estado(self) -> None:
        """Intenta cerrar modales abiertos para dejar la app en estado base."""
        import pyautogui
        time.sleep(0.5)

        presses = 3
        while presses > 0:
            pyautogui.press("escape")
            time.sleep(0.5)
            presses -= 1

        try:
            cancel_btn = self._find_control_with_child(
                self.main_window,
                {"name": self.selectors["cancel_button_child_name"]},
                timeout=2,
            )
            cancel_btn.Click()
            time.sleep(0.5)
        except RuntimeError:
            pass

        try:
            msgbox = self._find_control(
                self.main_window,
                {"auto_id": self.selectors["message_box_auto_id"]},
                timeout=1,
            )
            cancel = self._find_control(
                msgbox,
                {"auto_id": self.selectors["message_box_cancel_auto_id"]},
                timeout=1,
            )
            cancel.Click()
            time.sleep(0.3)
        except RuntimeError:
            pass

        # Cerrar diálogo "Guardar como" colgado (top-level)
        try:
            dlg = self._find_control_anywhere(
                {"name": "Guardar como", "control_type": "WindowControl"},
                timeout=2,
            )
            cerrar = self._find_control_anywhere(
                {"auto_id": "2", "control_type": "ButtonControl"},
                timeout=1,
            )
            cerrar.Click()
            time.sleep(0.5)
            log.info("Diálogo Guardar como colgado cerrado")
        except RuntimeError:
            pass

        log.info("Estado limpiado después de fallo")

    def cerrar_app(self) -> None:
        """Cierra MirSpiro."""
        try:
            if self.main_window:
                self.main_window.GetWindowPattern().Close()
        except Exception:
            pass
