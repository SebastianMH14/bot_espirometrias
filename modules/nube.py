import sys
import os
import time
from datetime import date, timedelta, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging

import config

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logger = logging.getLogger("bot_espirometrias")

DEBUG_DIR = os.path.join(config.BASE_DIR, "debug")
os.makedirs(DEBUG_DIR, exist_ok=True)


def _diagnostic(driver, tag):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{tag}"
    ss_path = os.path.join(DEBUG_DIR, f"{name}.png")
    html_path = os.path.join(DEBUG_DIR, f"{name}.html")
    driver.save_screenshot(ss_path)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(driver.page_source)
    logger.error("Debug guardado: %s | %s", ss_path, html_path)


def init_browser():
    download_dir = str(Path(config.DOWNLOAD_DIR).resolve())
    os.makedirs(download_dir, exist_ok=True)

    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "directory_upgrade": True,
        "safebrowsing.enabled": False,
    }
    opts = webdriver.ChromeOptions()
    opts.add_experimental_option("prefs", prefs)
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--start-maximized")
    return webdriver.Chrome(options=opts)


def login(driver):
    driver.get(config.URL_NUBE)
    WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='email']"))
    ).send_keys(config.USUARIO)

    driver.find_element(By.CSS_SELECTOR, "input[name='password']").send_keys(config.PASSWORD)
    driver.find_element(By.XPATH, "//button[contains(text(), 'Ingresar')]").click()

    WebDriverWait(driver, 15).until(
        lambda d: "login" not in d.current_url.lower() and "auth" not in d.current_url.lower()
    )
    logger.info("Login exitoso - %s", driver.current_url)
    return driver


def seleccionar_servicios(driver):
    WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.XPATH, "//label[contains(text(), 'Servicio(s)')]"))
    )

    WebDriverWait(driver, 15).until(
        lambda d: d.execute_script("""
            var labels = document.querySelectorAll('label');
            for (var i = 0; i < labels.length; i++) {
                if (labels[i].textContent.trim().toLowerCase().includes('servicio(s)')) {
                    var fg = labels[i].closest('.form-group');
                    if (fg && fg.querySelector('.select2-container')) return true;
                }
            }
            return false;
        """)
    )

    # Add <option selected> directly (no trigger to avoid Select2 resetting them)
    ok = driver.execute_script("""
        var labels = document.querySelectorAll('label');
        var select = null;
        for (var i = 0; i < labels.length; i++) {
            if (labels[i].textContent.trim().toLowerCase().includes('servicio(s)')) {
                var fg = labels[i].closest('.form-group');
                if (fg) select = fg.querySelector('select[name="servicio_id[]"]');
                break;
            }
        }
        if (!select) return false;
        var data = [
            {value: '25', text: 'ESPIROMETRIA O CURVA DE FLUJO  VOLUMEN PRE Y POST BRONCODILA'},
            {value: '26', text: 'ESPIROMETRIA Y CURVA DE FLUJO VOLUMEN SIMPLE'},
        ];
        for (var j = 0; j < data.length; j++) {
            var o = new Option(data[j].text, data[j].value);
            o.selected = true;
            select.appendChild(o);
        }
        return true;
    """)

    if not ok:
        logger.error("Error agregando opciones al select")
        _diagnostic(driver, "select_add_fail")
        return False

    logger.info("Servicios seleccionados OK")
    return True


def validar_servicios_seleccionados(driver):
    select = driver.find_element(By.NAME, "servicio_id[]")
    selected = [o.text.strip() for o in select.find_elements(By.TAG_NAME, "option") if o.is_selected()]
    tags = driver.find_elements(By.CSS_SELECTOR, ".select2-selection__choice")
    expected = [
        "ESPIROMETRIA O CURVA DE FLUJO  VOLUMEN PRE Y POST BRONCODILA",
        "ESPIROMETRIA Y CURVA DE FLUJO VOLUMEN SIMPLE",
    ]

    def norm(s):
        return " ".join(s.split())

    ok = (
        len(selected) == 2
        and len(tags) == 2
        and all(any(norm(e) == norm(s) for s in selected) for e in expected)
    )

    if ok:
        logger.info("VALIDACION OK")
        return True

    logger.error("VALIDACION FALLIDA: selected=%s tags=%d", selected, len(tags))
    _diagnostic(driver, "validation_fail")
    return False


def _esperar_descarga(download_dir, timeout=120):
    """Poll download_dir for a new .xlsx file. Returns the file path."""
    before = {p.name for p in Path(download_dir).iterdir() if p.suffix == ".xlsx"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        after = {p.name for p in Path(download_dir).iterdir() if p.suffix == ".xlsx"}
        new_files = after - before
        if new_files:
            # Return the newest
            candidates = [Path(download_dir) / n for n in new_files]
            latest = max(candidates, key=os.path.getmtime)
            # Wait for download to finish (file size stable)
            size = -1
            while time.time() < deadline:
                if latest.stat().st_size > 0 and latest.stat().st_size == size:
                    return str(latest)
                size = latest.stat().st_size
                time.sleep(0.5)
            return str(latest)
        time.sleep(1)
    return None


def seleccionar_todas_sedes(driver, wait) -> bool:
    """
    Abre el modal de sedes, hace clic en 'Ver Todas' y confirma.
    """
    try:
        btn_sede = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "a.btnCurrentSede"))
        )
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", btn_sede)
        time.sleep(0.5)
        try:
            btn_sede.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn_sede)

        wait.until(
            EC.presence_of_element_located((By.ID, "containerSedesCambiar"))
        )
        time.sleep(1)

        btn_ver_todas = wait.until(
            EC.element_to_be_clickable((By.ID, "btnVertodas"))
        )
        btn_ver_todas.click()
        logger.info("✅ Click en 'Ver Todas'")
        time.sleep(1)

        # Algunas versiones cierran el modal automáticamente al hacer clic
        # en "Ver Todas". Intentar confirmar solo si el botón sigue presente.
        try:
            btn_confirmar = wait.until(
                EC.element_to_be_clickable((By.ID, "btnConfirmaCambioSede"))
            )
            try:
                btn_confirmar.click()
            except Exception:
                driver.execute_script("arguments[0].click();", btn_confirmar)
            logger.info("✅ Confirmado 'Ver Todas'")
        except Exception:
            logger.info("⏭ Modal se cerró solo tras 'Ver Todas' (sin confirmación necesaria)")
        return True
    except Exception as e:
        logger.warning("⚠ No se pudo seleccionar 'Ver Todas': %s", e)
        return False


def descargar_reporte(driver, fecha_inicio: date | None = None, fecha_fin: date | None = None):
    logger.info("Abriendo reporte: %s", config.URL_REPORTE)
    driver.get(config.URL_REPORTE)

    if fecha_inicio is None:
        fecha_inicio = date.today()
    if fecha_fin is None:
        fecha_fin = date.today()

    wait = WebDriverWait(driver, 15)
    wait.until(
        EC.presence_of_element_located((By.XPATH, "//label[contains(text(), 'Servicio(s)')]"))
    )
    wait.until(
        lambda d: d.execute_script("""
            var labels = document.querySelectorAll('label');
            for (var i = 0; i < labels.length; i++) {
                if (labels[i].textContent.trim().toLowerCase().includes('servicio(s)')) {
                    var fg = labels[i].closest('.form-group');
                    if (fg && fg.querySelector('.select2-container')) return true;
                }
            }
            return false;
        """)
    )

    # Seleccionar todas las sedes para que el reporte incluya a todos
    seleccionar_todas_sedes(driver, wait)

    fecha_ini_str = fecha_inicio.isoformat()
    fecha_fin_str = fecha_fin.isoformat()

    # Sunu a veces suspende del todo la generación de este reporte desde su
    # backend (visto el 2026-09-10: banner "Generación suspendida ... Estamos
    # trabajando para restablecerla" en vez del formulario). En ese caso no
    # hay nada que hacer de nuestro lado — cortar rápido con un mensaje claro
    # en vez de un timeout confuso de 15s + "Cannot read properties of null".
    suspendido = driver.execute_script("""
        var texto = document.body.innerText || '';
        return texto.toLowerCase().includes('generación suspendida')
            || texto.toLowerCase().includes('generacion suspendida');
    """)
    if suspendido:
        logger.error(
            "Sunu tiene suspendida la generación del reporte de estado de "
            "atenciones (aviso del propio sitio). No es un error nuestro: "
            "hay que esperar a que Sunu la restablezca."
        )
        _diagnostic(driver, "reporte_suspendido")
        return None

    # Esperar a que el formulario del reporte exista en el DOM antes de
    # tocarlo. Sin esto, en algunas cargas más lentas de la página
    # (ej. tras actualizarse Chrome) "formReporte" todavía es null cuando
    # se ejecuta el script de abajo, lanzando "Cannot read properties of
    # null (reading 'querySelector')" y abortando todo el Módulo 1.
    wait.until(
        lambda d: d.execute_script(
            "return document.getElementById('formReporte') !== null;"
        )
    )

    # All form setup + submit in one shot
    resultado = driver.execute_script(f"""
        var form = document.getElementById('formReporte');
        if (!form) return 'formReporte no encontrado';

        // Clear any existing selected options on servicio_id select
        var sel = form.querySelector('select[name="servicio_id[]"]');
        if (sel) {{
            for (var j = sel.options.length - 1; j >= 0; j--) {{
                sel.remove(j);
            }}
        }}

        // Add hidden inputs for service IDs (bypass Select2)
        var vals = ['25', '26'];
        for (var j = 0; j < vals.length; j++) {{
            var inp = document.createElement('input');
            inp.type = 'hidden';
            inp.name = 'servicio_id[]';
            inp.value = vals[j];
            form.appendChild(inp);
        }}

        // Set dates: usar la API del plugin bootstrap-datepicker (no basta
        // con asignar .value: el plugin mantiene su propio estado interno
        // de fecha y lo puede sobreescribir con su valor por defecto si no
        // se usa su API) — pero si el plugin todavía no está inicializado
        // sobre el campo, caer a .value en vez de lanzar una excepción.
        function setFecha(id, valor) {{
            var el = document.getElementById(id);
            if (!el) return;
            try {{
                if ($(el).data('datepicker')) {{
                    $(el).datepicker('setDate', valor);
                    return;
                }}
            }} catch (e) {{}}
            el.value = valor;
        }}
        setFecha('fecha_inicio', '{fecha_ini_str}');
        setFecha('fecha_fin', '{fecha_fin_str}');

        // Set filter
        var f = document.querySelector('#filtros');
        if (f) {{
            for (var i = 0; i < f.options.length; i++) {{
                f.options[i].selected = f.options[i].value === '3';
            }}
        }}

        // Submit
        var btn = document.querySelector('button.btnSubmitReportes');
        if (btn) btn.click();
        return 'ok';
    """)
    if resultado != 'ok':
        logger.error("Error preparando formulario de reporte: %s", resultado)
        return None

    # Wait for download
    download_dir = Path(config.DOWNLOAD_DIR).resolve()
    os.makedirs(download_dir, exist_ok=True)
    ruta = _esperar_descarga(download_dir)

    if not ruta:
        logger.error("No se detectó descarga después de 120s")
        _diagnostic(driver, "download_timeout")
        return None

    ts = datetime.now().strftime("%H%M%S")
    p = Path(ruta)
    new_name = p.parent / f"{p.stem}_{ts}{p.suffix}"
    os.rename(ruta, new_name)
    logger.info("Excel guardado: %s", new_name)
    return str(new_name)
