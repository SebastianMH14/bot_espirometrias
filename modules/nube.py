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


def descargar_reporte(driver):
    logger.info("Abriendo reporte: %s", config.URL_REPORTE)
    driver.get(config.URL_REPORTE)

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

    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # All form setup + submit in one shot
    driver.execute_script(f"""
        var form = document.getElementById('formReporte');

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

        // Set dates
        document.getElementById('fecha_inicio').value = '{yesterday}';
        document.getElementById('fecha_fin').value = '{yesterday}';

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
    """)

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
