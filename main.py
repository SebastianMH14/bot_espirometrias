import json
import smtplib
import sys
import time
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from pathlib import Path

import config
from modules.excel import leer_excel, filtrar_por_sede, guardar_pacientes
from modules.logger import setup_logger
from modules.mirspiro_module import MirSpiroAutomation, es_error_savepdf
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
        return []

    try:
        driver = init_browser()
        login(driver)
        ruta_excel = descargar_reporte(driver)
        driver.quit()
    except Exception as e:
        logger.error("Error en automatización web: %s", e)
        return []

    if not ruta_excel:
        logger.error("No se descargó ningún archivo Excel")
        return []

    try:
        pacientes = leer_excel(ruta_excel)
    except Exception as e:
        logger.error("Error al leer el Excel: %s", e)
        return []

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

    try:
        auto.conectar()
    except Exception as e:
        logger.error("No se pudo conectar a MirSpiro: %s", e)
        return {"ok": 0, "fallos": len(pacientes), "detalles": []}

    resultados = {"ok": 0, "fallos": 0, "detalles": [], "exitosos": []}
    resumen_path = Path(config.DATA_DIR) / "resultados_mirspiro.json"

    ERROR_NO_REINTENTABLE = "Paciente no encontrado en MirSpiro"
    deadline = time.monotonic() + 3600

    # ── Recuperación ante fallo sistémico de MirSpiro ──
    MAX_CONSECUTIVE_SAVEPDF_FAIL = 2
    MAX_APP_RESTARTS = 3
    consecutive_savepdf_failures = 0
    app_restarts = 0

    for i, pac in enumerate(pacientes, 1):
        if time.monotonic() > deadline:
            logger.warning("Tiempo máximo de ejecución alcanzado. Abortando Módulo 2.")
            break

        cedula = str(pac.get("cedula", ""))
        nombre = pac.get("nombre", pac.get("NOMBRE_DEL_PACIENTE", ""))

        logger.info("[%d/%d] %s - %s", i, len(pacientes), cedula, nombre)

        res = auto.procesar_paciente(cedula)

        if not res["success"] and res["error"] != ERROR_NO_REINTENTABLE:
            logger.warning(
                "[%d/%d] Error retryable: %s. Reintentando en 3s…",
                i, len(pacientes), res["error"],
            )
            time.sleep(3)
            auto.limpiar_estado()
            res = auto.procesar_paciente(cedula)
            if res["success"]:
                logger.info("[%d/%d] Reintento exitoso para %s", i, len(pacientes), cedula)

        # ── Recuperación: reiniciar MirSpiro si savePdfBtn falla sistemáticamente ──
        if not res["success"] and es_error_savepdf(res.get("error")):
            consecutive_savepdf_failures += 1
            logger.warning(
                "Fallo savePdfBtn consecutivo #%d para %s",
                consecutive_savepdf_failures, cedula,
            )

            if consecutive_savepdf_failures >= MAX_CONSECUTIVE_SAVEPDF_FAIL:
                if app_restarts < MAX_APP_RESTARTS:
                    logger.warning(
                        "Patrón de fallo savePdfBtn (%d seguidos). Reiniciando MirSpiro...",
                        consecutive_savepdf_failures,
                    )
                    ok = auto.reiniciar()
                    if ok:
                        app_restarts += 1
                        consecutive_savepdf_failures = 0
                        logger.info("Reintentando %s tras reinicio de MirSpiro", cedula)
                        res = auto.procesar_paciente(cedula)
                        if res["success"]:
                            logger.info(
                                "[%d/%d] Exitoso tras reinicio: %s",
                                i, len(pacientes), cedula,
                            )
                    else:
                        logger.critical(
                            "No se pudo reiniciar MirSpiro. Abortando Módulo 2."
                        )
                        break
                else:
                    logger.critical(
                        "Máximo de reinicios de MirSpiro alcanzado (%d). Abortando.",
                        MAX_APP_RESTARTS,
                    )
                    break
        elif res["success"]:
            consecutive_savepdf_failures = 0
        # Otros errores (no savePdfBtn) no incrementan el contador

        if res["success"]:
            resultados["ok"] += 1
            resultados["exitosos"].append(cedula)
        else:
            resultados["fallos"] += 1
            resultados["detalles"].append(
                {"cedula": cedula, "nombre": nombre, "error": res["error"]}
            )

        with open(resumen_path, "w", encoding="utf-8") as f:
            json.dump(resultados, f, indent=2, ensure_ascii=False)

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

    # Solo procesar PDFs de la ejecución actual (evita acumulados)
    cedulas_ok: list[str] = []
    mirspiro_path = Path(config.DATA_DIR) / "resultados_mirspiro.json"
    if mirspiro_path.exists():
        with open(mirspiro_path, encoding="utf-8") as f:
            data = json.load(f)
        cedulas_ok = data.get("exitosos", [])

    if not cedulas_ok:
        logger.info("No hay PDFs exitosos de MirSpiro para subir. Omitiendo Módulo 3.")
        return {"exitosos": [], "ya_cargados": [], "pendientes": []}

    try:
        driver = init_browser()
        from modules.nube import login as nube_login
        nube_login(driver)
        from selenium.webdriver.support.ui import WebDriverWait
        wait = WebDriverWait(driver, 15)

        deadline_s3 = time.monotonic() + 3600
        res = procesar_carga_pdfs(
            driver=driver,
            wait=wait,
            carpeta_pdfs=config.PDF_DIR,
            fecha_objetivo=fecha_objetivo,
            deadline=deadline_s3,
            cedulas=cedulas_ok,
        )
        driver.quit()
        return res
    except Exception as e:
        logger.error("Error en Módulo 3: %s", e)
        return {"exitosos": [], "ya_cargados": [], "pendientes": []}


def _enviar_reporte_email(logger, sede, fecha, mirspiro_res, sunu_res):
    """Envía el resumen final por correo."""
    if not all([config.EMAIL_REMITENTE, config.EMAIL_PASSWORD, config.EMAIL_DESTINATARIOS]):
        logger.warning("Configuración de email incompleta, no se envió reporte")
        return

    asunto = f"Reporte diario Bot Espirometrías - {sede} - {fecha}"
    body_parts = [
        f"Sede: {sede}",
        f"Fecha objetivo: {fecha}",
        "",
        "── Módulo MirSpiro ──",
        f"  OK:     {mirspiro_res.get('ok', 0)}",
        f"  Fallos: {mirspiro_res.get('fallos', 0)}",
    ]
    for d in mirspiro_res.get("detalles", []):
        body_parts.append(f"  - {d['cedula']}: {d['error']}")

    body_parts.extend([
        "",
        "── Módulo Sunu ──",
        f"  Subidos:      {len(sunu_res.get('exitosos', []))}",
        f"  Ya cargados:  {len(sunu_res.get('ya_cargados', []))}",
        f"  Pendientes:   {len(sunu_res.get('pendientes', []))}",
    ])
    for p in sunu_res.get("pendientes", []):
        body_parts.append(f"  - {p['cedula']}: {p['motivo']}")

    body = "\n".join(body_parts)

    msg = MIMEMultipart()
    msg["From"] = config.EMAIL_REMITENTE
    msg["To"] = config.EMAIL_DESTINATARIOS
    msg["Subject"] = asunto
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        server = smtplib.SMTP(config.EMAIL_SMTP_HOST, config.EMAIL_SMTP_PORT)
        server.starttls()
        server.login(config.EMAIL_REMITENTE, config.EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.info("Reporte enviado por correo a %s", config.EMAIL_DESTINATARIOS)
    except Exception as e:
        logger.error("Error al enviar reporte por correo: %s", e)


def main():
    logger = setup_logger()

    fecha_objetivo = date.today()
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

    _generar_reporte_local(
        logger, config.SEDE_LOCAL, fecha_objetivo.isoformat(),
        resultados_mirspiro, resultados_sunu,
    )

    _enviar_reporte_email(
        logger, config.SEDE_LOCAL, fecha_objetivo.isoformat(),
        resultados_mirspiro, resultados_sunu,
    )

    logger.info("=== FIN ===")


def _generar_reporte_local(logger, sede, fecha, mirspiro_res, sunu_res):
    """Guarda reporte en texto en data/ independientemente del email."""
    lines = [
        "=" * 48,
        f"  REPORTE DIARIO - {sede}",
        f"  Fecha objetivo: {fecha}",
        "=" * 48,
        "",
    ]

    exitosos = sunu_res.get("exitosos", [])
    ya_cargados = sunu_res.get("ya_cargados", [])
    pendientes = sunu_res.get("pendientes", [])

    if exitosos:
        lines.append(f"--- CÉDULAS CARGADAS EXITOSAMENTE ({len(exitosos)}) ---")
        lines.append("")
        for i, c in enumerate(exitosos, 1):
            lines.append(f"  {i:3d}.  {c}")
        lines.append("")
        lines.append("-" * 48)

    if ya_cargados:
        lines.append(f"--- YA CARGADOS PREVIAMENTE ({len(ya_cargados)}) ---")
        lines.append("")
        for c in ya_cargados:
            lines.append(f"  - {c}")
        lines.append("")
        lines.append("-" * 48)

    if mirspiro_res.get("detalles"):
        lines.append("--- FALLOS EN MIRSPIRO ---")
        lines.append("")
        for d in mirspiro_res["detalles"]:
            lines.append(f"  - {d['cedula']}: {d['error']}")
        lines.append("")
        lines.append("-" * 48)

    if pendientes:
        lines.append(f"--- PENDIENTES DE SUBIR A SUNU ({len(pendientes)}) ---")
        lines.append("")
        for p in pendientes:
            lines.append(f"  - {p['cedula']}: {p['motivo']}")
        lines.append("")
        lines.append("-" * 48)

    lines.extend([
        f"  Total pacientes:     {mirspiro_res.get('ok', 0) + mirspiro_res.get('fallos', 0)}",
        f"  PDFs generados:      {mirspiro_res.get('ok', 0)}",
        f"  Subidos a Sunu:      {len(exitosos)}",
        f"  Ya cargados:         {len(ya_cargados)}",
        f"  Pendientes Sunu:     {len(pendientes)}",
        "=" * 48,
    ])

    path = Path(config.DATA_DIR) / f"reporte_{fecha}_{sede.lower().replace(' ', '_').replace(',', '')}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Reporte local guardado en %s", path)


if __name__ == "__main__":
    main()
