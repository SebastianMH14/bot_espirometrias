import json
import sys
from datetime import date, timedelta

from pathlib import Path

import config
from modules.excel import leer_excel, filtrar_por_sede, guardar_pacientes
from modules.logger import setup_logger
from modules.mirspiro_module import MirSpiroAutomation
from modules.nube import init_browser, login, descargar_reporte
from modules.subir_sunu import procesar_carga_pdfs


def modulo_1(logger) -> list[dict]:
    """Descarga el reporte y retorna la lista de pacientes filtrados por sede."""
    logger.info("=== MÓDULO 1: Descarga de reporte y filtrado ===")
    logger.info("Sede local: %s", config.SEDE_LOCAL)

    if not all([config.URL_NUBE, config.USUARIO, config.PASSWORD, config.SEDE_LOCAL]):
        logger.error(
            "Faltan variables de entorno. Revisa el archivo .env "
            "(URL_NUBE, USUARIO, PASSWORD, SEDE_LOCAL)"
        )
        sys.exit(1)

    try:
        driver = init_browser()
        login(driver)
        ruta_excel = descargar_reporte(driver)
        driver.quit()
    except Exception as e:
        logger.error("Error en automatización web: %s", e)
        sys.exit(1)

    if not ruta_excel:
        logger.error("No se descargó ningún archivo Excel")
        sys.exit(1)

    try:
        pacientes = leer_excel(ruta_excel)
    except Exception as e:
        logger.error("Error al leer el Excel: %s", e)
        sys.exit(1)

    pacientes_sede = filtrar_por_sede(pacientes, config.SEDE_LOCAL)

    if not pacientes_sede:
        logger.warning(
            "No hay pacientes pendientes para %s. Se genera JSON vacío.",
            config.SEDE_LOCAL,
        )

    guardar_pacientes(pacientes_sede)
    logger.info("Módulo 1 completado: %d pacientes", len(pacientes_sede))
    return pacientes_sede


def modulo_2(logger, pacientes: list[dict]) -> dict:
    """Procesa cada paciente en MirSpiro y genera su PDF."""
    logger.info("=== MÓDULO 2: Automatización MirSpiro ===")

    if not pacientes:
        logger.warning("No hay pacientes para procesar en MirSpiro")
        return {"ok": 0, "fallos": 0, "detalles": []}

    auto = MirSpiroAutomation(
        sede=config.SEDE_LOCAL,
        output_dir=config.PDF_DIR,
        executable_path=config.MIRSPIRO_EXE or None,
        typing_delay=config.MIRSPIRO_TYPING_DELAY,
    )

    # Conectar UNA vez al inicio
    try:
        auto.conectar()
    except Exception as e:
        logger.error("No se pudo conectar a MirSpiro: %s", e)
        return {"ok": 0, "fallos": len(pacientes), "detalles": []}

    resultados = {"ok": 0, "fallos": 0, "detalles": []}

    for i, pac in enumerate(pacientes, 1):
        cedula = str(pac.get("cedula", ""))
        nombre = pac.get("nombre", pac.get("NOMBRE_DEL_PACIENTE", ""))

        logger.info("[%d/%d] %s - %s", i, len(pacientes), cedula, nombre)

        res = auto.procesar_paciente(cedula)

        if res["success"]:
            resultados["ok"] += 1
        else:
            resultados["fallos"] += 1
            resultados["detalles"].append(
                {"cedula": cedula, "nombre": nombre, "error": res["error"]}
            )

    auto.cerrar_app()
    logger.info(
        "Módulo 2 completado: %d OK, %d fallos",
        resultados["ok"],
        resultados["fallos"],
    )
    return resultados


def modulo_3(logger, fecha_objetivo: date) -> dict:
    """Sube los PDFs generados a los perfiles de los pacientes en Sunu."""
    logger.info("=== MÓDULO 3: Carga de PDFs a Sunu ===")

    try:
        driver = init_browser()
        from modules.nube import login as nube_login
        nube_login(driver)
        from selenium.webdriver.support.ui import WebDriverWait
        wait = WebDriverWait(driver, 15)

        res = procesar_carga_pdfs(
            driver=driver,
            wait=wait,
            carpeta_pdfs=config.PDF_DIR,
            fecha_objetivo=fecha_objetivo,
        )
        driver.quit()
        return res
    except Exception as e:
        logger.error("Error en Módulo 3: %s", e)
        return {"exitosos": [], "pendientes": []}


def main():
    logger = setup_logger()

    fecha_objetivo = date.today() - timedelta(days=1)
    logger.info("Fecha objetivo: %s", fecha_objetivo)

    pacientes = modulo_1(logger)
    resultados_mirspiro = modulo_2(logger, pacientes)

    resumen_path = Path(config.DATA_DIR) / "resultados_mirspiro.json"
    with open(resumen_path, "w", encoding="utf-8") as f:
        json.dump(resultados_mirspiro, f, indent=2, ensure_ascii=False)
    logger.info("Resumen MirSpiro guardado en %s", resumen_path)

    resultados_sunu = modulo_3(logger, fecha_objetivo)

    resumen_final_path = Path(config.DATA_DIR) / "resultados_final.json"
    with open(resumen_final_path, "w", encoding="utf-8") as f:
        json.dump({
            "mirspiro": resultados_mirspiro,
            "sunu": resultados_sunu,
            "fecha_objetivo": fecha_objetivo.isoformat(),
            "sede": config.SEDE_LOCAL,
        }, f, indent=2, ensure_ascii=False)
    logger.info("Resumen final guardado en %s", resumen_final_path)

    logger.info("=== FIN ===")


if __name__ == "__main__":
    main()
