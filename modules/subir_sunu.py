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
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import logging

import config
from modules.circuit_breaker import CircuitBreaker

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


def _build_pdf_filename(cedula: str, fecha: date | None = None) -> str:
    """Construye el nombre del PDF. Sin fecha → solo cédula (backward compat)."""
    if fecha is None:
        return f"{cedula}.pdf"
    return f"{cedula}_{fecha.isoformat()}.pdf"


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
    Navega a /pacientes, busca por cédula usando el buscador global
    ("Búsqueda global de pacientes", widget pgs__*) y abre el perfil.

    Nota: Sunu rediseñó /pacientes (≈2026-08-31): el listado ul#lista-pacientes
    fue reemplazado por una tabla paginada (#pacientes-table) + un buscador
    global tipo modal (button.pgs__trigger → input.pgs__input → resultados en
    #patient-global-search-list → enlace "Abrir perfil" a /pacientes/{id}).
    El perfil del paciente en sí (pestañas, tabla de espirometría) no cambió.

    Raises:
        TimeoutException: si no se encuentra el paciente.
    """
    driver.get(URL_PACIENTES)
    cedula_num = re.sub(r"[^\d]", "", cedula)

    trigger = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "button.pgs__trigger"))
    )
    trigger.click()

    pgs_input = wait.until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "input.pgs__input"))
    )
    pgs_input.send_keys(cedula_num)

    # Esperar un estado terminal real: hay resultados, o el widget confirma
    # explícitamente que no encontró nada. Cualquier otro texto (contador de
    # caracteres, "Escribe para buscar", etc.) es transitorio y no es fiable.
    wait.until(
        lambda d: d.find_elements(
            By.CSS_SELECTOR, "#patient-global-search-list button.pgs__row"
        )
        or "No encontramos" in d.find_element(
            By.ID, "patient-global-search-list"
        ).text
    )

    resultados = driver.find_elements(
        By.CSS_SELECTOR, "#patient-global-search-list button.pgs__row"
    )
    resultado = None
    for r in resultados:
        try:
            doc_text = r.find_element(By.CSS_SELECTOR, ".pgs__row-document").text
        except NoSuchElementException:
            continue
        if re.sub(r"[^\d]", "", doc_text) == cedula_num:
            resultado = r
            break

    if resultado is None:
        raise TimeoutException(
            f"Paciente {cedula_num} no encontrado en búsqueda global de Sunu"
        )

    resultado.click()
    logger.debug("Resultado de búsqueda global clickeado: %s", cedula_num)

    perfil_link = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "a.pgs__open-profile"))
    )
    perfil_link.click()
    logger.debug("Perfil abierto vía búsqueda global: %s", cedula_num)

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
    Busca en #tab-espirometria #table tbody la fila cuya primera celda
    coincida con fecha_objetivo.

    Args:
        fecha_objetivo: fecha a buscar (formato DD/MM/AAAA en la tabla).

    Returns:
        (fila_encontrada: WebElement | None, aria_id: str | None)
        Si no encuentra la fila, retorna (None, None).

    Note:
        La tabla es un DataTable de jQuery con paginación. Si la fecha no está
        en la primera página, avanza a las siguientes páginas hasta encontrarla
        o hasta que no haya más páginas.

        El id "table" se repite en varias pestañas del perfil del paciente
        (HTML inválido pero real en Sunu) — por eso todos los selectores acá
        se anclan a #tab-espirometria para no leer la tabla de otra pestaña.
    """
    fecha_str = fecha_objetivo.strftime("%d/%m/%Y")
    logger.debug("Buscando fila con fecha %s…", fecha_str)

    try:
        wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#tab-espirometria #table tbody")
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
    filas = driver.find_elements(By.CSS_SELECTOR, "#tab-espirometria #table tbody tr")
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
            "#tab-espirometria #table_paginate .paginate_button.next:not(.disabled)"
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

def _ya_cargado(driver: WebDriver) -> bool:
    """Detecta si el modal de adjuntos muestra que el PDF ya fue cargado previamente.

    Cuando ya existe un adjunto, el modal reemplaza el input de subida por
    div.adjuntos-formato-proceso (tabla de estado + iframe.visorPdfAdjuntoFormato
    con el PDF cargado, pendiente de lectura/firma).
    """
    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "div.adjuntos-formato-proceso, iframe.visorPdfAdjuntoFormato",
        )
        return True
    except NoSuchElementException:
        pass
    try:
        body = driver.find_element(By.CSS_SELECTOR, "div.modal-body")
        texto = (body.text or "").lower()
        indicios = ["ya cargado", "archivo cargado", "adjunto cargado", "cargado anteriormente"]
        if any(i in texto for i in indicios):
            return True
    except Exception:
        pass
    try:
        driver.find_element(
            By.CSS_SELECTOR, "a.adjunto-link, a.btnVerArchivo, a.link-adjunto"
        )
        return True
    except NoSuchElementException:
        pass
    return False


def subir_pdf_adjunto(
    driver: WebDriver,
    wait: WebDriverWait,
    cedula: str,
    pdf_path: str,
) -> str | None:
    """
    Abre el modal de adjuntos desde la fila de espirometría y sube el PDF.

    Args:
        pdf_path: ruta absoluta al archivo PDF {cedula}.pdf.

    Returns:
        "ok" si se subió correctamente,
        "ya_cargado" si el PDF ya estaba cargado previamente,
        "modal_no_abrio" si el modal/input de subida nunca apareció,
        "boton_adjuntos_no_encontrado" / "boton_cargar_no_encontrado" /
        "pdf_no_existe" / "sin_confirmacion" para los demás casos de error.
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
        return "boton_adjuntos_no_encontrado"

    # Esperar que el modal se abra y el input file esté presente
    try:
        file_input = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input.inputAdjuntoFormatoPdf")
            )
        )
    except TimeoutException:
        if _ya_cargado(driver):
            logger.info("PDF ya cargado previamente para %s", cedula)
            _cerrar_modal_si_abierto(driver)
            return "ya_cargado"
        logger.warning("Modal no se abrió o input file no encontrado")
        _diagnostic(driver, "modal_no_abrio")
        _cerrar_modal_si_abierto(driver)
        return "modal_no_abrio"

    # Adjuntar el PDF (ruta absoluta)
    if not os.path.isfile(pdf_path):
        logger.warning("PDF no existe: %s", pdf_path)
        _cerrar_modal_si_abierto(driver)
        return "pdf_no_existe"

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
        return "boton_cargar_no_encontrado"

    # Esperar confirmación de subida exitosa
    exito = _esperar_confirmacion_subida(driver, wait)

    if not exito:
        logger.warning("No se detectó confirmación de subida para %s", cedula)
        _diagnostic(driver, "subida_fail")
        _cerrar_modal_si_abierto(driver)
        return "sin_confirmacion"

    logger.info("PDF subido exitosamente para cédula %s", cedula)
    _cerrar_modal_si_abierto(driver)
    return "ok"


def _esperar_confirmacion_subida(
    driver: WebDriver,
    wait: WebDriverWait,
    timeout: int = 30,
) -> bool:
    """
    Espera señales de que la subida del PDF se completó.

    Estrategias (en orden de confianza):
      A. Aparece un mensaje de éxito en .estadoSubidaAdjuntoFormato
         (con texto que no contenga "error").
      B. El modal se cierra automáticamente (modal de Bootstrap ya no está visible).
      C. El spinner .fa-spin se oculta (vuelve a tener class "hidden").
         Solo se acepta si además no hay texto de error visible.

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
        # A. Mensaje de éxito en .estadoSubidaAdjuntoFormato
        try:
            estado = driver.find_element(
                By.CSS_SELECTOR, ".estadoSubidaAdjuntoFormato"
            )
            texto = (estado.text or "").lower()
            if texto and "error" not in texto:
                return True
        except NoSuchElementException:
            pass

        # B. Modal cerrado automáticamente
        try:
            modales = driver.find_elements(
                By.CSS_SELECTOR, "div.modal.in, div.modal.fade.in, div.modal.show"
            )
            if not modales:
                return True
        except Exception:
            pass

        # C. Spinner oculto + sin texto de error visible
        try:
            spinner = driver.find_element(
                By.CSS_SELECTOR, "button.btnSubirAdjuntoFormato i.fa-spin"
            )
            if "hidden" in (spinner.get_attribute("class") or ""):
                try:
                    estado = driver.find_element(
                        By.CSS_SELECTOR, ".estadoSubidaAdjuntoFormato"
                    )
                    if "error" not in (estado.text or "").lower():
                        return True
                except NoSuchElementException:
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

def _driver_vivo(driver: WebDriver) -> bool:
    """Verifica que el driver de Selenium sigue respondiendo."""
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False


def _heartbeat(driver: WebDriver, logger, label: str = "") -> None:
    """Refresca la sesión de Selenium para evitar timeout de inactividad."""
    try:
        driver.execute_script("void(0);")
    except Exception:
        logger.warning("Heartbeat falló (%s): sesión posiblemente muerta", label)


def procesar_carga_pdfs(
    driver: WebDriver,
    wait: WebDriverWait,
    carpeta_pdfs: str | Path,
    fecha_objetivo: date | None = None,
    deadline: float | None = None,
    cedulas: list[str] | None = None,
    items: list[dict[str, Any]] | None = None,
) -> dict:
    """
    Sube los PDFs de los pacientes a Sunu.

    Dos modos de uso:

    **Modo legacy** (items=None):
      - Si cedulas se proporciona, filtra la carpeta por esos stems.
      - Si no, procesa todos los PDFs de la carpeta.
      - Usa fecha_objetivo para todas las filas (fallback: date.today()).

    **Modo items** (items es una lista de dicts):
      - Cada item tiene {"cedula": str, "fecha": date|None}.
      - Construye el nombre del PDF como {cedula}_{fecha}.pdf (o solo {cedula}.pdf si fecha es None).
      - Usa la fecha de cada item para buscar la fila en la tabla.
      - Si un item tiene fecha=None, usa fecha_objetivo como fallback.

    Args:
        carpeta_pdfs: directorio donde están los PDFs.
        fecha_objetivo: fecha de atención (modo legacy, o fallback para items sin fecha).
        cedulas: lista de cédulas a procesar (solo modo legacy).
        items: lista de {"cedula": ..., "fecha": ...}.

    Returns:
        dict con exitosos, ya_cargados y pendientes.
    """
    carpeta = Path(carpeta_pdfs)
    if not carpeta.is_dir():
        logger.error("La carpeta de PDFs no existe: %s", carpeta)
        return {"exitosos": [], "pendientes": []}

    # ── Normalizar entrada a pdf_info: list of (pdf_path, cedula, fecha) ──
    pdf_info: list[tuple[Path, str, date]] = []

    if items is not None:
        for item in items:
            c = item["cedula"]
            f = item.get("fecha") or fecha_objetivo
            if f is None:
                logger.warning("Ítem sin fecha para cédula %s — omitiendo", c)
                continue
            pdf_name = _build_pdf_filename(c, f)
            pdf_path = carpeta / pdf_name
            if pdf_path.is_file():
                pdf_info.append((pdf_path, c, f))
            else:
                # Fallback: intentar solo con cédula (compatibilidad con PDFs viejos)
                pdf_old = carpeta / f"{c}.pdf"
                if pdf_old.is_file():
                    logger.info("Usando PDF legacy para %s: %s", c, pdf_old.name)
                    pdf_info.append((pdf_old, c, f))
                else:
                    logger.warning("PDF no encontrado para cédula %s: %s", c, pdf_name)
    elif cedulas is not None:
        _fecha = fecha_objetivo or date.today()
        pdfs = sorted(p for p in carpeta.glob("*.pdf") if p.stem in cedulas)
        if len(pdfs) < len(cedulas):
            faltantes = set(cedulas) - {p.stem for p in pdfs}
            for c in faltantes:
                logger.warning("PDF no encontrado para cédula %s", c)
        pdf_info = [(p, p.stem, _fecha) for p in pdfs]
    else:
        _fecha = fecha_objetivo or date.today()
        pdfs = sorted(carpeta.glob("*.pdf"))
        pdf_info = [(p, p.stem, _fecha) for p in pdfs]

    if not pdf_info:
        logger.warning("No hay PDFs pendientes en %s", carpeta)
        return {"exitosos": [], "pendientes": []}

    logger.info("=== FASE 3: Carga de %d PDFs a Sunu ===", len(pdf_info))
    exitosos: list[str] = []
    ya_cargados: list[str] = []
    pendientes: list[dict] = []

    breaker = CircuitBreaker(umbral=5)
    abortado_temprano: str | None = None
    progreso_path = Path(config.DATA_DIR) / "resultados_sunu_parcial.json"
    intentos_reinicio_navegador = 0
    MAX_REINICIOS_NAVEGADOR = 2

    def _guardar_progreso() -> None:
        try:
            with open(progreso_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"exitosos": exitosos, "ya_cargados": ya_cargados, "pendientes": pendientes},
                    f, indent=2, ensure_ascii=False,
                )
        except OSError as e:
            logger.debug("No se pudo guardar progreso parcial: %s", e)

    for idx, (pdf_path, cedula, fecha_item) in enumerate(pdf_info, 1):
        if deadline and time.monotonic() > deadline:
            logger.warning("Tiempo máximo de ejecución alcanzado. Abortando Módulo 3.")
            break

        if not _driver_vivo(driver):
            if intentos_reinicio_navegador >= MAX_REINICIOS_NAVEGADOR:
                logger.error(
                    "Driver de Selenium no responde y se agotaron los reinicios "
                    "(%d). Abortando Módulo 3.", MAX_REINICIOS_NAVEGADOR,
                )
                break
            intentos_reinicio_navegador += 1
            logger.warning(
                "Driver de Selenium no responde. Reintentando navegador (%d/%d)…",
                intentos_reinicio_navegador, MAX_REINICIOS_NAVEGADOR,
            )
            try:
                try:
                    driver.quit()
                except Exception:
                    pass
                from modules.nube import init_browser, login as nube_login
                driver = init_browser()
                nube_login(driver)
                wait = WebDriverWait(driver, 15)
                logger.info("Navegador reiniciado correctamente, continuando lote…")
            except Exception as e:
                logger.error("No se pudo reiniciar el navegador: %s. Abortando Módulo 3.", e)
                break

        if idx % 5 == 0:
            _heartbeat(driver, logger, f"lote_{idx}")

        logger.info("[%d/%d] Carga para cédula %s…", idx, len(pdf_info), cedula)
        causa_fallo: str | None = None

        try:
            # ── 5a. Abrir perfil ──
            try:
                abrir_paciente(driver, wait, cedula)
            except TimeoutException:
                logger.warning("Paciente %s no encontrado en Sunu", cedula)
                pendientes.append({
                    "cedula": cedula,
                    "motivo": MotivoPendiente.PACIENTE_NO_ENCONTRADO,
                    "fecha": fecha_item.isoformat() if fecha_item else None,
                })
                causa_fallo = MotivoPendiente.PACIENTE_NO_ENCONTRADO.value
                continue

            # ── 5b. Click pestaña Espirometría ──
            _click_pestania_espirometria(driver, wait)

            # ── 5c. Buscar fila por fecha ──
            fila, aria_id = buscar_fila_espirometria(driver, wait, fecha_item)
            if fila is None:
                logger.warning(
                    "Fecha %s no encontrada para %s",
                    fecha_item.strftime("%d/%m/%Y"),
                    cedula,
                )
                pendientes.append({
                    "cedula": cedula,
                    "motivo": MotivoPendiente.FECHA_NO_ENCONTRADA,
                    "fecha": fecha_item.isoformat() if fecha_item else None,
                })
                causa_fallo = MotivoPendiente.FECHA_NO_ENCONTRADA.value
                continue

            logger.debug("Fila encontrada con aria_id=%s", aria_id)

            # ── 5d. Subir PDF ──
            pdf_ruta = str(pdf_path.resolve())
            res_upload = subir_pdf_adjunto(driver, wait, cedula, pdf_ruta)
            if res_upload == "ok":
                exitosos.append(cedula)
            elif res_upload == "ya_cargado":
                ya_cargados.append(cedula)
            else:
                motivo = (
                    MotivoPendiente.MODAL_NO_ABRIO
                    if res_upload == "modal_no_abrio"
                    else MotivoPendiente.ERROR_SUBIDA_PDF
                )
                pendientes.append({
                    "cedula": cedula,
                    "motivo": motivo,
                    "fecha": fecha_item.isoformat() if fecha_item else None,
                })
                causa_fallo = motivo.value

        except TimeoutException as e:
            logger.warning("Timeout procesando %s: %s", cedula, e)
            pendientes.append({
                "cedula": cedula,
                "motivo": MotivoPendiente.TIMEOUT,
                "fecha": fecha_item.isoformat() if fecha_item else None,
            })
            causa_fallo = MotivoPendiente.TIMEOUT.value
            _cerrar_modal_si_abierto(driver)
            _diagnostic(driver, f"timeout_{cedula}")

        except Exception as e:
            logger.exception("Error inesperado con %s: %s", cedula, e)
            pendientes.append({
                "cedula": cedula,
                "motivo": MotivoPendiente.ERROR_INESPERADO,
                "fecha": fecha_item.isoformat() if fecha_item else None,
            })
            causa_fallo = MotivoPendiente.ERROR_INESPERADO.value
            _cerrar_modal_si_abierto(driver)
            _diagnostic(driver, f"error_{cedula}")

        finally:
            # finally corre siempre, incluso cuando el bloque try hizo
            # "continue" arriba (paciente no encontrado / fecha no encontrada)
            # — si no, esos casos nunca guardarían progreso ni pasarían por
            # el circuit breaker. El "break" de más abajo también es válido
            # acá: si el breaker dispara, corta el "continue" pendiente.
            _guardar_progreso()
            alerta = breaker.registrar(ok=causa_fallo is None, causa=causa_fallo)
            if alerta:
                logger.critical(alerta)
                abortado_temprano = alerta
                break

    # ── Segunda pasada: reintentar pendientes recuperables ──
    MOTIVOS_REINTENTABLES = {
        MotivoPendiente.ERROR_SUBIDA_PDF,
        MotivoPendiente.TIMEOUT,
        MotivoPendiente.ERROR_INESPERADO,
        MotivoPendiente.MODAL_NO_ABRIO,
    }
    retryables = [] if abortado_temprano else [
        p for p in pendientes if p["motivo"] in MOTIVOS_REINTENTABLES
    ]

    if retryables:
        logger.info(
            "Segunda pasada: reintentando %d de %d pendientes…",
            len(retryables), len(pendientes),
        )
        time.sleep(3)
        for p in retryables:
            cedula = p["cedula"]

            fecha_retry = None
            if "fecha" in p and p["fecha"]:
                try:
                    fecha_retry = date.fromisoformat(p["fecha"])
                except (ValueError, TypeError):
                    pass
            if fecha_retry is None:
                fecha_retry = fecha_objetivo or date.today()

            pdf_retry = carpeta / _build_pdf_filename(cedula, fecha_retry)
            if not pdf_retry.is_file():
                pdf_retry_old = carpeta / f"{cedula}.pdf"
                if pdf_retry_old.is_file():
                    pdf_retry = pdf_retry_old
                    logger.info("Usando PDF legacy para reintento: %s", pdf_retry.name)
                else:
                    logger.warning("PDF no encontrado para reintento: %s", pdf_retry)
                    continue

            logger.info("[reintento] Carga para cédula %s…", cedula)
            try:
                abrir_paciente(driver, wait, cedula)
                _click_pestania_espirometria(driver, wait)
                fila, aria_id = buscar_fila_espirometria(driver, wait, fecha_retry)
                if fila is None:
                    logger.warning("[reintento] Fecha no encontrada para %s", cedula)
                    continue
                ok_retry = subir_pdf_adjunto(driver, wait, cedula, str(pdf_retry.resolve()))
                if ok_retry == "ok":
                    pendientes.remove(p)
                    exitosos.append(cedula)
                    logger.info("[reintento] Exitoso para %s", cedula)
                elif ok_retry == "ya_cargado":
                    pendientes.remove(p)
                    ya_cargados.append(cedula)
                    logger.info("[reintento] Ya cargado para %s", cedula)
            except TimeoutException as e:
                logger.warning("[reintento] Timeout en %s: %s", cedula, e)
                _cerrar_modal_si_abierto(driver)
            except Exception as e:
                logger.exception("[reintento] Error en %s: %s", cedula, e)
                _cerrar_modal_si_abierto(driver)
                _diagnostic(driver, f"retry_{cedula}")

    # ── Log final ──
    logger.info(
        "FASE 3 completada: %d exitosos, %d ya cargados, %d pendientes",
        len(exitosos),
        len(ya_cargados),
        len(pendientes),
    )
    for p in pendientes:
        logger.warning(
            "Pendiente - cédula: %s, motivo: %s",
            p["cedula"], p["motivo"],
        )

    resultado = {
        "exitosos": exitosos,
        "ya_cargados": ya_cargados,
        "pendientes": pendientes,
    }
    if abortado_temprano:
        resultado["abortado_temprano"] = abortado_temprano
    return resultado
