"""
MirSpiro Automation Module
==========================
Automatización RPA de escritorio para MirSpiro.

ESTRATEGIA:
  - uiautomation (UI Automation via COM, sin dependencia de pywin32)
  - Funciona en Python 3.12+ / 3.14+
  - Selectores configurables vía dict DEFAULT_SELECTORS
  - Esperas por existencia/habilitación, retry con backoff
  - Logging detallado por paso

FLUJO REAL:
  1. Abrir MirSpiro
  2. Escribir cédula en input "Buscar pacientes" + Enter
  3. Click "Imprimir" → modal Print Preview
  4. Click "Guardar PDF" → diálogo Guardar como
  5. Guardar como {cedula}_{YYYYMMDD}_{HHMMSS}.pdf
  6. Click "Cancelar" para cerrar modal
  7. Siguiente paciente (misma ventana abierta)
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
# Selectores UI por defecto
# ──────────────────────────────────────────────
DEFAULT_SELECTORS: dict[str, Any] = {
    "main_window_title": "Mir Spiro",
    "startup_button_text": "Continuar",
    "startup_disabled_timeout": 12,
    "search_field_auto_id": "ptbSearch",
    "print_button_auto_id": "printButton",
    "guardar_pdf_button_text": "Guardar PDF",
    "cancel_button_text": "Cancelar",
    "save_dialog_title": "Guardar como",
    "save_button_text": "Guardar",
    "overwrite_confirm_title_re": "Confirmar sobreescritura|Confirmación",
}


def _build_pdf_filename(cedula: str) -> str:
    return f"{cedula}.pdf"


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

        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── helpers ────────────────────────────────────────────

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
            # Buscar ventana cuyo título contenga "Mir Spiro"
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

        # ── Cerrar modal de suscripción ──
        log.info("Cerrando modal de suscripción…")
        self._cerrar_startup_modal()
        log.info("Modal procesado, continuando…")

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

    def _cerrar_startup_modal(self) -> None:
        """Cierra el modal de suscripción con pyautogui (no expone controles UIA)."""
        import pyautogui

        log.info("Esperando 12s para que 'Continuar' se habilite…")
        time.sleep(12)

        rect = self.main_window.BoundingRectangle
        if not rect:
            log.warning("No se pudieron obtener bounds de la ventana")
            return

        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top

        # "Continuar" está abajo a la derecha del modal
        # Modal ocupa ~60% del ancho, centrado
        # Botón a ~65% del ancho de la ventana, ~93% del alto
        click_x = rect.left + int(win_w * 0.645)
        click_y = rect.top + int(win_h * 0.88)

        log.info("Click en 'Continuar' en (%d, %d) [ventana %dx%d en (%d,%d)]",
                 click_x, click_y, win_w, win_h, rect.left, rect.top)
        pyautogui.click(click_x, click_y)
        time.sleep(1)

    # ── 2. Búsqueda de paciente ───────────────────────────

    def buscar_paciente(self, cedula: str) -> None:
        """Escribe la cédula en el campo 'Buscar pacientes' y presiona Enter."""
        import pyautogui

        sel = self.selectors
        log.info("Buscando paciente %s…", cedula)

        search = self._retry(self._find_search_field, sel)
        rect = search.BoundingRectangle

        if rect:
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            # Click directo en la "x" de limpiar (borde derecho del search field)
            # "x" ~90.7% ancho, ~41.7% alto
            x_click = rect.left + int(w * 0.907)
            y_click = rect.top + int(h * 0.417)
            pyautogui.click(x_click, y_click)
            log.debug("Click en 'x' de limpieza en (%d, %d)", x_click, y_click)
        else:
            search.Click()

        time.sleep(0.15)

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
        self._esperar_y_click_guardar_pdf()
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
        btn_aid = sel.get("print_button_auto_id")
        if btn_aid:
            btn = self._retry(self._find_control, self.main_window, {"auto_id": btn_aid})
        else:
            btn = self._retry(self._find_control, self.main_window, {"name": "Imprimir"})
        btn.Click()
        log.info("Click en 'Imprimir'")
        time.sleep(1)

    def _esperar_y_click_guardar_pdf(self) -> None:
        """Espera el modal de impresión y hace clic en 'Guardar PDF' con pyautogui."""
        import pyautogui

        log.info("Esperando modal de impresión…")
        time.sleep(2)

        rect = self.main_window.BoundingRectangle
        if not rect:
            raise RuntimeError("No se pudieron obtener bounds de la ventana")

        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top

        # Coordenadas de "Guardar PDF" (medidas: x=1150, y=633 con ventana 1240x768 en 180,42)
        click_x = rect.left + int(win_w * 0.782)
        click_y = rect.top + int(win_h * 0.77)

        log.info("Click en 'Guardar PDF' en (%d, %d)", click_x, click_y)
        pyautogui.click(click_x, click_y)
        time.sleep(1)

    def _guardar_como(self, pdf_path: Path, sel: dict) -> None:
        """Escribe la ruta completa en el diálogo Guardar como usando pyautogui."""
        import pyautogui

        # Eliminar PDF existente para evitar diálogo de sobrescritura
        if pdf_path.exists():
            pdf_path.unlink()
            log.debug("PDF existente eliminado: %s", pdf_path)

        log.info("Esperando diálogo Guardar como…")
        time.sleep(1)

        # Seleccionar todo (Ctrl+A), escribir ruta, Enter
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.typewrite(str(pdf_path.resolve()), interval=0.02)
        time.sleep(0.3)
        pyautogui.press("enter")
        time.sleep(2)

    def _cerrar_modal_impresion(self) -> None:
        """Cierra el modal de impresión (botón Cancelar) con pyautogui."""
        import pyautogui

        rect = self.main_window.BoundingRectangle
        if not rect:
            log.warning("No se pudieron obtener bounds, cerrando con Escape")
            pyautogui.press("escape")
            time.sleep(1)
            return

        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top

        click_x = rect.left + int(win_w * 0.766)
        click_y = rect.top + int(win_h * 0.857)

        log.info("Click en 'Cancelar' en (%d, %d)", click_x, click_y)
        pyautogui.click(click_x, click_y)
        time.sleep(1)

    # ── Flujo completo ────────────────────────────────────

    def _paciente_encontrado(self) -> bool:
        """Verifica si la búsqueda encontró un paciente (printButton habilitado)."""
        try:
            btn = self._find_control(
                self.main_window,
                {"auto_id": self.selectors["print_button_auto_id"]},
                timeout=3,
            )
            return bool(btn.IsEnabled)
        except Exception:
            return False

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
            result["error"] = str(e)
        return result

    def cerrar_app(self) -> None:
        """Cierra MirSpiro."""
        try:
            if self.main_window:
                self.main_window.GetWindowPattern().Close()
        except Exception:
            pass
