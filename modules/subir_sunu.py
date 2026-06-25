"""
Módulo 3 — Carga de PDFs de Espirometría a Sunu

Flujo por paciente:
  1. abrir_paciente() — navega al perfil del paciente por cédula
  2. Click en pestaña "Espirometría"
  3. buscar_fila_espirometria() — localiza la fila de la tabla que coincide con fecha_objetivo
  4. subir_pdf_adjunto() — abre modal de adjuntos, sube el PDF, espera confirmación
  5. procesar_carga_pdfs() — orquesta el lote completo
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import date, datetime
from enum import Enum
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging

import config

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

logger = logging.getLogger("bot_espirometrias")

URL_PACIENTES = config.URL_NUBE.rstrip("/") + "/pacientes"

DEBUG_DIR = os.path.join(config.BASE_DIR, "debug")


def _diagnostic(driver, tag: str) -> None:
    """Guarda screenshot + HTML en debug/ para diagnóstico."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{ts}_{tag}"
        ss_path = os.path.join(DEBUG_DIR, f"{name}.png")
        html_path = os.path.join(DEBUG_DIR, f"{name}.html")
        driver.save_screenshot(ss_path)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        logger.info("Debug guardado: %s | %s", ss_path, html_path)
    except Exception:
        pass


class MotivoPendiente(str, Enum):
    """Causas por las que un paciente queda sin procesar en Fase 3."""
    PACIENTE_NO_ENCONTRADO = "PACIENTE_NO_ENCONTRADO"
    FECHA_NO_ENCONTRADA = "FECHA_NO_ENCONTRADA"
    MODAL_NO_ABRIO = "MODAL_NO_ABRIO"
    ERROR_SUBIDA_PDF = "ERROR_SUBIDA_PDF"
    SIN_PDF_LOCAL = "SIN_PDF_LOCAL"
    TIMEOUT = "TIMEOUT"
    ERROR_INESPERADO = "ERROR_INESPERADO"


# ── 1. Apertura de perfil ───────────────────────────────────

def abrir_paciente(driver: WebDriver, wait: WebDriverWait, cedula: str) -> None:
    """
    Navega a la lista de pacientes, busca por cédula y abre el perfil.

    Raises:
        TimeoutException: si no se encuentra el paciente.
    """
    driver.get(URL_PACIENTES)
    time.sleep(3)

    inp = wait.until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, "input[type='search']"))
    )
    cedula_num = re.sub(r"[^\d]", "", cedula)
    inp.clear()
    inp.send_keys(cedula_num)

    resultado = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH,
             f"//ul[@id='lista-pacientes']//a[contains(@class,'list-group-item') "
             f"and contains(.,'{cedula_num}')]")
        )
    )
    driver.execute_script("arguments[0].click();", resultado)
    logger.debug("Paciente abierto: %s", cedula_num)

    wait.until(
        EC.presence_of_element_located(
            (By.XPATH, "//a[@href='#tab-citas']")
        )
    )
    wait.until(
        EC.presence_of_element_located(
            (By.XPATH, "//a[@href='#tab-notas-enfermeria']")
        )
    )
    logger.debug("Perfil del paciente %s completamente cargado", cedula_num)


# ── 2. Navegación a pestaña Espirometría ────────────────────

def _click_pestania_espirometria(driver: WebDriver, wait: WebDriverWait) -> None:
    """Hace clic en la pestaña 'Espirometría' del perfil del paciente."""
    tab = wait.until(
        EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "a.link-tab[href='#tab-espirometria']")
        )
    )
    driver.execute_script("arguments[0].click();", tab)
    logger.debug("Pestaña Espirometría clickeada")


# ── 3. Búsqueda de fila por fecha ──────────────────────────

def buscar_fila_espirometria(
    driver: WebDriver,
    wait: WebDriverWait,
    fecha_objetivo: date,
) -> tuple:
    """
    Busca en #table tbody la fila cuya primera celda coincida con fecha_objetivo.

    Args:
        fecha_objetivo: fecha a buscar (formato DD/MM/AAAA en la tabla).

    Returns:
        (fila_encontrada: WebElement | None, aria_id: str | None)
        Si no encuentra la fila, retorna (None, None).

    Note:
        La tabla es un DataTable de jQuery con paginación. Si la fecha no está
        en la primera página, avanza a las siguientes páginas hasta encontrarla
        o hasta que no haya más páginas.
    """
    fecha_str = fecha_objetivo.strftime("%d/%m/%Y")
    logger.debug("Buscando fila con fecha %s…", fecha_str)

    try:
        wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#table tbody")
            )
        )
    except TimeoutException:
        logger.warning("No se encontró la tabla de espirometrías")
        return None, None

    max_paginas = 20
    for _ in range(max_paginas):
        fila, aria_id = _buscar_en_pagina_actual(driver, fecha_str)
        if fila is not None:
            return fila, aria_id

        if not _ir_siguiente_pagina(driver, wait):
            break

    logger.debug("Fecha %s no encontrada en la tabla", fecha_str)
    return None, None


def _buscar_en_pagina_actual(driver: WebDriver, fecha_str: str) -> tuple:
    """Busca la fecha_str en la página actual de la tabla DataTable."""
    filas = driver.find_elements(By.CSS_SELECTOR, "#table tbody tr")
    for fila in filas:
        try:
            celdas = fila.find_elements(By.TAG_NAME, "td")
            if not celdas:
                continue
            texto_fecha = celdas[0].text.strip()
            if texto_fecha == fecha_str:
                btn = fila.find_element(
                    By.CSS_SELECTOR, "a.btnVerAdjuntosFormato"
                )
                aria_id = btn.get_attribute("aria_id")
                return fila, aria_id
        except NoSuchElementException:
            continue
    return None, None


def _ir_siguiente_pagina(driver: WebDriver, wait: WebDriverWait) -> bool:
    """
    Si la tabla DataTable tiene un botón 'Siguiente' habilitado, hace clic y
    espera a que la tabla se actualice. Retorna True si se movió a la
    siguiente página.
    """
    try:
        next_btn = driver.find_element(
            By.CSS_SELECTOR,
            "#table_paginate .paginate_button.next:not(.disabled)"
        )
        if next_btn.is_enabled():
            driver.execute_script("arguments[0].click();", next_btn)
            time.sleep(0.5)
            logger.debug("Avanzando a siguiente página de la tabla")
            return True
    except NoSuchElementException:
        pass
    return False


# ── 4. Subida de PDF en modal de adjuntos ──────────────────

def subir_pdf_adjunto(
    driver: WebDriver,
    wait: WebDriverWait,
    cedula: str,
    pdf_path: str,
) -> bool:
    """
    Abre el modal de adjuntos desde la fila de espirometría y sube el PDF.

    Args:
        pdf_path: ruta absoluta al archivo PDF {cedula}.pdf.

    Returns:
        True si la subida se completó exitosamente, False en caso de error.
    """
    # Buscar el botón de adjuntos (ya debería estar visible)
    try:
        btn_adjuntos = driver.find_element(
            By.CSS_SELECTOR, "a.btnVerAdjuntosFormato"
        )
        driver.execute_script("arguments[0].click();", btn_adjuntos)
        logger.debug("Modal de adjuntos abierto")
    except NoSuchElementException:
        logger.warning("Botón de adjuntos no encontrado")
        return False

    # Esperar que el modal se abra y el input file esté presente
    try:
        file_input = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input.inputAdjuntoFormatoPdf")
            )
        )
    except TimeoutException:
        logger.warning("Modal no se abrió o input file no encontrado")
        _diagnostic(driver, "modal_no_abrio")
        _cerrar_modal_si_abierto(driver)
        return False

    # Adjuntar el PDF (ruta absoluta)
    if not os.path.isfile(pdf_path):
        logger.warning("PDF no existe: %s", pdf_path)
        _cerrar_modal_si_abierto(driver)
        return False

    file_input.send_keys(os.path.abspath(pdf_path))
    logger.debug("PDF adjuntado al input file: %s", pdf_path)

    # Click en botón "Cargar PDF"
    try:
        btn_subir = driver.find_element(
            By.CSS_SELECTOR, "button.btnSubirAdjuntoFormato"
        )
        btn_subir.click()
        logger.debug("Click en 'Cargar PDF'")
    except NoSuchElementException:
        logger.warning("Botón 'Cargar PDF' no encontrado")
        _cerrar_modal_si_abierto(driver)
        return False

    # Esperar confirmación de subida exitosa
    exito = _esperar_confirmacion_subida(driver, wait)

    if not exito:
        logger.warning("No se detectó confirmación de subida para %s", cedula)
        _diagnostic(driver, "subida_fail")
        _cerrar_modal_si_abierto(driver)
        return False

    logger.info("PDF subido exitosamente para cédula %s", cedula)
    _cerrar_modal_si_abierto(driver)
    return True


def _esperar_confirmacion_subida(
    driver: WebDriver,
    wait: WebDriverWait,
    timeout: int = 30,
) -> bool:
    """
    Espera señales de que la subida del PDF se completó.

    Estrategias (cualquiera que ocurra primero):
      A. El spinner .fa-spin se oculta (vuelve a tener class "hidden").
      B. Aparece un mensaje de éxito en .estadoSubidaAdjuntoFormato
         (con texto que no contenga "error").
      C. El modal se cierra automáticamente o aparece un mensaje de recarga
         en .adjuntos-formato-proceso.

    TODO: Confirmar el selector exacto de éxito inspeccionando la respuesta
    real. Opciones alternativas:
      - Esperar a que desaparezca un modal de Bootstrap
        (EC.invisibility_of_element_located((By.CSS_SELECTOR, ".modal.in")))
      - Esperar a que aparezca una alerta-success dentro del modal
        (EC.presence_of_element_located((By.CSS_SELECTOR, ".alert-success")))
      - Esperar a que el atributo data-reload-route se dispare en el contenedor
    """
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        # A. Spinner oculto
        try:
            spinner = driver.find_element(
                By.CSS_SELECTOR, "button.btnSubirAdjuntoFormato i.fa-spin"
            )
            if "hidden" in (spinner.get_attribute("class") or ""):
                return True
        except NoSuchElementException:
            pass

        # B. Mensaje de éxito en .estadoSubidaAdjuntoFormato
        try:
            estado = driver.find_element(
                By.CSS_SELECTOR, ".estadoSubidaAdjuntoFormato"
            )
            texto = (estado.text or "").lower()
            if texto and "error" not in texto:
                return True
        except NoSuchElementException:
            pass

        time.sleep(0.5)

    return False


def _cerrar_modal_si_abierto(driver: WebDriver) -> None:
    """
    Cierra cualquier modal de Bootstrap abierto, para no bloquear
    la siguiente iteración.
    """
    try:
        modales = driver.find_elements(
            By.CSS_SELECTOR, "div.modal.in, div.modal.fade.in, div.modal.show"
        )
        for modal in modales:
            close_btn = modal.find_elements(
                By.CSS_SELECTOR, "[data-dismiss='modal'], .close"
            )
            if close_btn:
                driver.execute_script("arguments[0].click();", close_btn[0])
                time.sleep(0.3)
                return

        # Fallback: tecla ESC
        from selenium.webdriver.common.keys import Keys
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        time.sleep(0.3)
    except Exception:
        pass


# ── 5. Orquestación del lote completo ───────────────────────

def procesar_carga_pdfs(
    driver: WebDriver,
    wait: WebDriverWait,
    carpeta_pdfs: str | Path,
    fecha_objetivo: date,
) -> dict:
    """
    Recorre todos los PDFs en carpeta_pdfs (nombrados como {cedula}.pdf) y
    para cada uno: abre perfil, va a espirometría, sube el PDF.

    Args:
        carpeta_pdfs: directorio donde están los PDFs generados por Módulo 2.
        fecha_objetivo: fecha de atención a buscar en la tabla de cada paciente.

    Returns:
        dict con:
          - exitosos: list[str] — cédulas cargadas correctamente
          - pendientes: list[dict] — {cedula, motivo} para los que fallaron
    """
    carpeta = Path(carpeta_pdfs)
    if not carpeta.is_dir():
        logger.error("La carpeta de PDFs no existe: %s", carpeta)
        return {"exitosos": [], "pendientes": []}

    pdfs = sorted(carpeta.glob("*.pdf"))
    if not pdfs:
        logger.warning("No hay PDFs pendientes en %s", carpeta)
        return {"exitosos": [], "pendientes": []}

    logger.info("=== FASE 3: Carga de %d PDFs a Sunu ===", len(pdfs))
    exitosos: list[str] = []
    pendientes: list[dict] = []

    for pdf_path in pdfs:
        cedula = pdf_path.stem  # filename sin extensión
        logger.info("Procesando carga para cédula %s…", cedula)

        try:
            # ── 5a. Abrir perfil ──
            try:
                abrir_paciente(driver, wait, cedula)
            except TimeoutException:
                logger.warning("Paciente %s no encontrado en Sunu", cedula)
                pendientes.append({
                    "cedula": cedula,
                    "motivo": MotivoPendiente.PACIENTE_NO_ENCONTRADO,
                })
                continue

            # ── 5b. Click pestaña Espirometría ──
            _click_pestania_espirometria(driver, wait)

            # ── 5c. Buscar fila por fecha ──
            fila, aria_id = buscar_fila_espirometria(driver, wait, fecha_objetivo)
            if fila is None:
                logger.warning(
                    "Fecha %s no encontrada para %s",
                    fecha_objetivo.strftime("%d/%m/%Y"),
                    cedula,
                )
                pendientes.append({
                    "cedula": cedula,
                    "motivo": MotivoPendiente.FECHA_NO_ENCONTRADA,
                })
                continue

            logger.debug("Fila encontrada con aria_id=%s", aria_id)

            # ── 5d. Subir PDF ──
            pdf_ruta = str(pdf_path.resolve())
            ok = subir_pdf_adjunto(driver, wait, cedula, pdf_ruta)
            if ok:
                exitosos.append(cedula)
            else:
                pendientes.append({
                    "cedula": cedula,
                    "motivo": MotivoPendiente.ERROR_SUBIDA_PDF,
                })

        except TimeoutException as e:
            logger.warning("Timeout procesando %s: %s", cedula, e)
            pendientes.append({
                "cedula": cedula,
                "motivo": MotivoPendiente.TIMEOUT,
            })
            _cerrar_modal_si_abierto(driver)
            _diagnostic(driver, f"timeout_{cedula}")

        except Exception as e:
            logger.exception("Error inesperado con %s: %s", cedula, e)
            pendientes.append({
                "cedula": cedula,
                "motivo": MotivoPendiente.ERROR_INESPERADO,
            })
            _cerrar_modal_si_abierto(driver)
            _diagnostic(driver, f"error_{cedula}")

    # ── Log final ──
    logger.info(
        "FASE 3 completada: %d exitosos, %d pendientes",
        len(exitosos),
        len(pendientes),
    )
    for p in pendientes:
        logger.warning(
            "Pendiente - cédula: %s, motivo: %s",
            p["cedula"], p["motivo"],
        )

    return {
        "exitosos": exitosos,
        "pendientes": pendientes,
    }
