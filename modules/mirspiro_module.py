"""
MirSpiro Automation Module
==========================
Automatización RPA de escritorio para la aplicación MirSpiro.

ESTRATEGIA:
  - pywinauto con backend UIA
  - Selectores configurables vía dict DEFAULT_SELECTORS
  - Resuelve controles por placeholder / automation_id / texto
  - Esperas inteligentes (wait visible/enabled), retry con backoff
  - Logging detallado por paso

FLUJO REAL (validado con operador):
  1. Abrir MirSpiro
  2. Escribir cédula en input "Buscar pacientes" → Enter
  3. Click botón "Imprimir" → se abre modal tipo Print Preview
  4. Click "Guardar PDF" → se abre diálogo Guardar como de Windows
  5. Escribir nombre y Guardar
  6. Click "Cancelar" para cerrar el modal
  7. Repetir con siguiente paciente (misma ventana abierta)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pywinauto import Application, WindowSpecification

log = logging.getLogger("bot_espirometrias.mirspiro")

# ──────────────────────────────────────────────
# Selectores UI por defecto
# ──────────────────────────────────────────────
DEFAULT_SELECTORS: dict[str, Any] = {
    "main_window_title": "MIR Spiro",
    "search_field_placeholder": "Buscar pacientes",
    "search_field_auto_id": None,
    "print_button_text": "Imprimir",
    "print_button_auto_id": None,
    "modal_title": "",                        # Se auto-detecta si está vacío
    "guardar_pdf_button_text": "Guardar PDF",
    "guardar_pdf_button_auto_id": None,
    "cancel_button_text": "Cancelar",
    "cancel_button_auto_id": None,
    "save_dialog_title": "Guardar como",
    "save_button_text": "Guardar",
    "overwrite_confirm_title_re": "Confirmar.*sobrescritura|Confirmación",
    "overwrite_yes_text": "Sí",
}


def _build_pdf_filename(cedula: str) -> str:
    now = datetime.now()
    return f"{cedula}_{now.strftime('%Y%m%d_%H%M%S')}.pdf"


class MirSpiroAutomation:
    """Controla MirSpiro vía UI Automation (RPA de escritorio)."""

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
        self.app: Application | None = None
        self.main_window: WindowSpecification | None = None

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

    # ── 1. Conexión / apertura ────────────────────────────

    def conectar(self, timeout: float = 30) -> None:
        """Inicia MirSpiro o se conecta si ya está en ejecución."""
        if self.executable_path:
            log.info("Iniciando MirSpiro desde %s", self.executable_path)
            self.app = Application(backend="uia").start(self.executable_path, timeout=timeout)
        else:
            log.info("Conectando a MirSpiro ya en ejecución…")
            self.app = Application(backend="uia").connect(
                title_re=self.selectors["main_window_title"], timeout=timeout
            )

        self.main_window = self.app.window(title_re=self.selectors["main_window_title"])
        self.main_window.wait("visible", timeout=timeout)
        self.main_window.wait("enabled", timeout=timeout)
        log.info("Ventana principal: '%s'", self.main_window.window_text())

    # ── 2. Búsqueda de paciente ───────────────────────────

    def buscar_paciente(self, cedula: str) -> None:
        """Escribe la cédula en el campo 'Buscar pacientes' y presiona Enter."""
        sel = self.selectors
        main = self.main_window
        log.info("Buscando paciente %s…", cedula)

        search = self._retry(self._find_search_field, main, sel)
        search.click_input()
        search.clear()

        for ch in cedula:
            search.type_keys(ch, pause=self.typing_delay)

        search.type_keys("{ENTER}")
        time.sleep(0.3)
        log.info("Búsqueda ejecutada para %s", cedula)

    @staticmethod
    def _find_search_field(main: WindowSpecification, sel: dict) -> Any:
        aid = sel.get("search_field_auto_id")
        placeholder = sel.get("search_field_placeholder")

        if aid:
            return main.child_window(auto_id=aid, control_type="Edit").wait("visible", timeout=5)

        if placeholder:
            for prop in ("help_text", "name"):
                try:
                    return main.child_window(**{prop: placeholder}, control_type="Edit").wait(
                        "visible", timeout=3
                    )
                except Exception:
                    continue

        return main.child_window(class_name="Edit").wait("visible", timeout=5)

    # ── 3. Imprimir → Guardar PDF ─────────────────────────

    def exportar_pdf(self, cedula: str) -> str:
        """
        Flujo:
          1. Click "Imprimir"
          2. En el modal → click "Guardar PDF"
          3. Se abre Guardar como → escribir ruta y Guardar
          4. Click "Cancelar" para cerrar el modal
        """
        sel = self.selectors
        pdf_name = _build_pdf_filename(cedula)
        pdf_path = self.output_dir / pdf_name
        log.info("Exportando PDF: %s", pdf_path)

        self._click_imprimir(sel)
        modal = self._esperar_modal(sel)
        self._click_guardar_pdf(modal, sel)

        # Guardar como
        self._guardar_como(pdf_path, sel)

        # Cerrar modal
        self._cerrar_modal(modal, sel)

        # Validación
        if not pdf_path.exists():
            raise RuntimeError(f"No se generó el PDF: {pdf_path}")
        if pdf_path.stat().st_size == 0:
            pdf_path.unlink(missing_ok=True)
            raise RuntimeError(f"PDF vacío: {pdf_path}")

        log.info("PDF OK: %s (%d bytes)", pdf_path, pdf_path.stat().st_size)
        return str(pdf_path.resolve())

    def _click_imprimir(self, sel: dict) -> None:
        btn_text = sel["print_button_text"]
        btn_aid = sel.get("print_button_auto_id")

        kwargs: dict[str, Any] = {"control_type": "Button"}
        if btn_text:
            kwargs["text"] = btn_text
        if btn_aid:
            kwargs["auto_id"] = btn_aid

        btn = self.main_window.child_window(**kwargs)
        btn.wait("visible", timeout=10)
        btn.click_input()
        log.info("Click en 'Imprimir'")

    def _esperar_modal(self, sel: dict) -> WindowSpecification:
        """Detecta el modal tipo Print Preview de MirSpiro."""
        # Intentar con título exacto si está configurado
        modal_title = sel.get("modal_title")
        if modal_title:
            try:
                modal = self.app.window(title=modal_title)
                modal.wait("visible", timeout=8)
                log.info("Modal detectado: '%s'", modal_title)
                return modal
            except Exception:
                pass

        # Auto-detección: buscar ventana con botón "Guardar PDF"
        for w in self.app.windows():
            try:
                w.child_window(text=sel["guardar_pdf_button_text"], control_type="Button").wait(
                    "visible", timeout=1
                )
                log.info("Modal auto-detectado: '%s'", w.window_text())
                return w
            except Exception:
                continue

        raise RuntimeError("No se encontró el modal con botón 'Guardar PDF'")

    def _click_guardar_pdf(self, modal: WindowSpecification, sel: dict) -> None:
        btn_text = sel["guardar_pdf_button_text"]
        btn_aid = sel.get("guardar_pdf_button_auto_id")

        kwargs: dict[str, Any] = {"control_type": "Button"}
        if btn_text:
            kwargs["text"] = btn_text
        if btn_aid:
            kwargs["auto_id"] = btn_aid

        btn = modal.child_window(**kwargs)
        btn.wait("visible", timeout=5)
        btn.click_input()
        log.info("Click en 'Guardar PDF'")

    def _guardar_como(self, pdf_path: Path, sel: dict) -> None:
        dlg_title = sel["save_dialog_title"]
        save_text = sel["save_button_text"]

        try:
            dlg = self.app.window(title=dlg_title)
            dlg.wait("visible", timeout=8)
        except Exception as e:
            raise RuntimeError(f"No apareció el diálogo 'Guardar como': {e}")

        # Campo de nombre de archivo
        try:
            filename_edit = dlg.child_window(class_name="Edit")
            filename_edit.wait("visible", timeout=3)
            filename_edit.clear()
            filename_edit.type_keys(str(pdf_path.resolve()), pause=0.02)
        except Exception:
            pass

        # Botón Guardar
        try:
            btn = dlg.child_window(text=save_text, control_type="Button")
            btn.wait("visible", timeout=3)
            btn.click_input()
        except Exception:
            dlg.type_keys("{ENTER}")

        time.sleep(1)

        # Confirmar sobrescritura si aparece
        try:
            confirm = self.app.window(title_re=sel["overwrite_confirm_title_re"])
            confirm.wait("visible", timeout=2)
            confirm.child_window(text=sel["overwrite_yes_text"], control_type="Button").click_input()
        except Exception:
            pass

    def _cerrar_modal(self, modal: WindowSpecification, sel: dict) -> None:
        cancel_text = sel["cancel_button_text"]
        cancel_aid = sel.get("cancel_button_auto_id")

        kwargs: dict[str, Any] = {"control_type": "Button"}
        if cancel_text:
            kwargs["text"] = cancel_text
        if cancel_aid:
            kwargs["auto_id"] = cancel_aid

        try:
            btn = modal.child_window(**kwargs)
            btn.wait("visible", timeout=3)
            btn.click_input()
            log.info("Modal cerrado con '%s'", cancel_text)
        except Exception:
            # Fallback: Escape o cerrar ventana
            try:
                modal.type_keys("{ESCAPE}")
                log.info("Modal cerrado con Escape")
            except Exception:
                pass

        time.sleep(0.3)

    # ── Flujo completo ────────────────────────────────────

    def procesar_paciente(self, cedula: str) -> dict:
        """
        Busca paciente, exporta PDF y cierra el modal.
        NO llama a conectar() — debe llamarse una vez antes de procesar todos.
        """
        result: dict = {"success": False, "pdf_path": None, "error": None}
        try:
            self.buscar_paciente(cedula)
            pdf_path = self.exportar_pdf(cedula)
            result["success"] = True
            result["pdf_path"] = pdf_path
        except Exception as e:
            log.exception("Error con paciente %s: %s", cedula, e)
            result["error"] = str(e)
        return result

    def cerrar_app(self) -> None:
        """Cierra la aplicación MirSpiro por completo."""
        try:
            self.main_window.close()
        except Exception:
            pass
        if self.app:
            try:
                self.app.kill()
            except Exception:
                pass
