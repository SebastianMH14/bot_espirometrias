import argparse
import json
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from pathlib import Path

import config
from modules.circuit_breaker import CircuitBreaker
from modules.excel import leer_excel, filtrar_por_sede, guardar_pacientes
from modules.logger import setup_logger
from modules.mirspiro_module import MirSpiroAutomation, es_error_savepdf
from modules.nube import init_browser, login, descargar_reporte
from modules.subir_sunu import procesar_carga_pdfs


def modulo_1(logger, fecha_inicio: date | None = None, fecha_fin: date | None = None) -> list[dict]:
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
        ruta_excel = descargar_reporte(driver, fecha_inicio, fecha_fin)
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


def modulo_2(logger, pacientes: list[dict], fecha_fallback: date | None = None) -> dict:
    """Procesa cada paciente en MirSpiro y genera su PDF.

    fecha_fallback: se usa para nombrar el PDF cuando el Excel no trae una
    fecha detectable para el paciente. Sin esto, el PDF se guarda como
    "{cedula}.pdf" (sin fecha), lo que puede sobreescribir un PDF de una
    visita anterior del mismo paciente entre ejecuciones distintas.
    """
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

    # Circuit breaker general: corta el lote si el MISMO error (que no sea
    # "paciente no encontrado", esperable por paciente) se repite muchas
    # veces seguidas — señal de que algo estructural se rompió, no de que
    # varios pacientes puntuales fallaron.
    breaker = CircuitBreaker(umbral=5)
    abortado_temprano: str | None = None

    for i, pac in enumerate(pacientes, 1):
        if time.monotonic() > deadline:
            logger.warning("Tiempo máximo de ejecución alcanzado. Abortando Módulo 2.")
            break

        cedula = str(pac.get("cedula", ""))
        nombre = pac.get("nombre", pac.get("NOMBRE_DEL_PACIENTE", ""))
        fecha_pac = pac.get("fecha") or fecha_fallback  # date o None

        logger.info("[%d/%d] %s - %s", i, len(pacientes), cedula, nombre)

        res = auto.procesar_paciente(cedula, fecha_pac)

        if not res["success"] and res["error"] != ERROR_NO_REINTENTABLE:
            logger.warning(
                "[%d/%d] Error retryable: %s. Reintentando en 3s…",
                i, len(pacientes), res["error"],
            )
            time.sleep(3)
            auto.limpiar_estado()
            res = auto.procesar_paciente(cedula, fecha_pac)
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
                        res = auto.procesar_paciente(cedula, fecha_pac)
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
            resultados["exitosos"].append({
                "cedula": cedula,
                "fecha": fecha_pac.isoformat() if hasattr(fecha_pac, "isoformat") else None,
            })
            breaker.registrar(ok=True)
        else:
            resultados["fallos"] += 1
            resultados["detalles"].append(
                {"cedula": cedula, "nombre": nombre, "error": res["error"]}
            )
            if res["error"] != ERROR_NO_REINTENTABLE:
                alerta = breaker.registrar(ok=False, causa=res["error"])
                if alerta:
                    logger.critical(alerta)
                    abortado_temprano = alerta

            if auto.pantalla_bloqueada():
                abortado_temprano = (
                    "La sesión de Windows está bloqueada (pantalla de bloqueo). "
                    "Abortando Módulo 2 para no desperdiciar reintentos: "
                    f"{len(pacientes) - i}/{len(pacientes)} pacientes sin procesar."
                )
                logger.critical(abortado_temprano)

        with open(resumen_path, "w", encoding="utf-8") as f:
            json.dump(resultados, f, indent=2, ensure_ascii=False)

        if abortado_temprano:
            break

    auto.cerrar_app()
    logger.info(
        "Módulo 2 completado: %d OK, %d fallos",
        resultados["ok"],
        resultados["fallos"],
    )
    if abortado_temprano:
        resultados["abortado_temprano"] = abortado_temprano
    return resultados


def modulo_3(logger, fecha_objetivo: date | None = None) -> dict:
    """Sube los PDFs generados a los perfiles de los pacientes en Sunu.

    Args:
        fecha_objetivo: fallback para items sin fecha (cuando el Excel no
                        tiene columna de fecha detectable).
    """
    logger.info("=== MÓDULO 3: Carga de PDFs a Sunu ===")

    # Solo procesar items de la ejecución actual (evita acumulados)
    mirspiro_path = Path(config.DATA_DIR) / "resultados_mirspiro.json"
    items_ok: list[dict] = []
    if mirspiro_path.exists():
        with open(mirspiro_path, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("exitosos", [])
        for entry in raw:
            if isinstance(entry, str):
                items_ok.append({"cedula": entry, "fecha": None})
            elif isinstance(entry, dict):
                f = entry.get("fecha")
                items_ok.append({
                    "cedula": entry["cedula"],
                    "fecha": date.fromisoformat(f) if f else None,
                })

    if not items_ok:
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
            items=items_ok,
        )
        driver.quit()
        return res
    except Exception as e:
        logger.error("Error en Módulo 3: %s", e)
        return {"exitosos": [], "ya_cargados": [], "pendientes": []}


def _enviar_reporte_email(logger, sede, fecha, mirspiro_res, sunu_res, alerta: str | None = None):
    """Envía el resumen final por correo.

    alerta: si se pasa, se antepone al asunto y al cuerpo como aviso crítico
    (ej. cuando un módulo se abortó temprano por fallo sistemático).
    Reintenta hasta 3 veces ante fallos de red/SMTP; si aun así no logra
    enviar, guarda el reporte en data/ para que no se pierda en silencio.
    """
    if not all([config.EMAIL_REMITENTE, config.EMAIL_PASSWORD, config.EMAIL_DESTINATARIOS]):
        logger.warning("Configuración de email incompleta, no se envió reporte")
        return

    prefijo_asunto = "[ALERTA] " if alerta else ""
    asunto = f"{prefijo_asunto}Reporte diario Bot Espirometrías - {sede} - {fecha}"
    body_parts = []
    if alerta:
        body_parts.extend([f"*** {alerta} ***", ""])
    body_parts += [
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

    intentos = 3
    for intento in range(1, intentos + 1):
        try:
            server = smtplib.SMTP(config.EMAIL_SMTP_HOST, config.EMAIL_SMTP_PORT, timeout=30)
            server.starttls()
            server.login(config.EMAIL_REMITENTE, config.EMAIL_PASSWORD)
            server.send_message(msg)
            server.quit()
            logger.info("Reporte enviado por correo a %s", config.EMAIL_DESTINATARIOS)
            return
        except Exception as e:
            logger.warning(
                "Intento %d/%d de envío de correo falló: %s", intento, intentos, e
            )
            if intento < intentos:
                time.sleep(10 * intento)

    logger.error(
        "No se pudo enviar el reporte por correo tras %d intentos. "
        "Guardando copia local para no perder la alerta.", intentos,
    )
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fallback_path = Path(config.DATA_DIR) / f"EMAIL_NO_ENVIADO_{ts}.txt"
    fallback_path.write_text(f"Asunto: {asunto}\n\n{body}", encoding="utf-8")
    logger.error("Copia local del reporte guardada en %s", fallback_path)


def _limpiar_debug_antiguo(logger, dias: int = None) -> None:
    """Purga archivos de debug/ más viejos que N días (contienen datos de pacientes)."""
    dias = config.DEBUG_RETENTION_DAYS if dias is None else dias
    carpeta = Path(config.DEBUG_DIR)
    if not carpeta.is_dir():
        return

    limite = time.time() - dias * 86400
    borrados = 0
    liberado = 0
    for f in carpeta.iterdir():
        if not f.is_file():
            continue
        try:
            if f.stat().st_mtime < limite:
                liberado += f.stat().st_size
                f.unlink()
                borrados += 1
        except OSError:
            continue

    if borrados:
        logger.info(
            "Limpieza de debug/: %d archivo(s) eliminados (>%d días), %.1f MB liberados",
            borrados, dias, liberado / (1024 * 1024),
        )


def main():
    parser = argparse.ArgumentParser(description="Bot Espirometrías — RPA Sunu + MirSpiro")
    parser.add_argument("--fecha", help="Fecha única (YYYY-MM-DD). Por defecto: hoy.")
    parser.add_argument("--desde", help="Inicio del rango (YYYY-MM-DD)")
    parser.add_argument("--hasta", help="Fin del rango (YYYY-MM-DD)")
    parser.add_argument("--solo-subir", nargs="?", const="scan", metavar="FECHA",
                        help="Escanea la carpeta de PDFs y solo ejecuta Módulo 3. "
                             "Si se pasa FECHA (YYYY-MM-DD), la usa para nombres legacy.")
    args = parser.parse_args()

    logger = setup_logger()
    _limpiar_debug_antiguo(logger)

    # ── Determinar rango de fechas ──
    if args.fecha:
        fecha_inicio = date.fromisoformat(args.fecha)
        fecha_fin = fecha_inicio
    elif args.desde and args.hasta:
        fecha_inicio = date.fromisoformat(args.desde)
        fecha_fin = date.fromisoformat(args.hasta)
    elif args.desde or args.hasta:
        parser.error("--desde y --hasta deben usarse juntos.")
    else:
        fecha_inicio = date.today()
        fecha_fin = fecha_inicio

    etiqueta_fecha = (
        fecha_inicio.isoformat()
        if fecha_inicio == fecha_fin
        else f"{fecha_inicio.isoformat()} ~ {fecha_fin.isoformat()}"
    )
    logger.info("Rango de fechas: %s", etiqueta_fecha)

    # ── Modo solo-subir (Módulo 3 únicamente, desde PDFs existentes) ──
    if args.solo_subir is not None:
        fecha_fallback = None
        if args.solo_subir != "scan":
            fecha_fallback = date.fromisoformat(args.solo_subir)
        logger.info("=== MODO SOLO SUBIR (escaneando PDFs existentes) ===")
        items_scan = _items_desde_pdfs(logger, fecha_fallback)

        # Escribir resultados_mirspiro.json simulado para que modulo_3 lo lea
        resumen_path = Path(config.DATA_DIR) / "resultados_mirspiro.json"
        resultados_mirspiro = {
            "ok": len(items_scan),
            "fallos": 0,
            "detalles": [],
            "exitosos": items_scan,
        }
        with open(resumen_path, "w", encoding="utf-8") as f:
            json.dump(resultados_mirspiro, f, indent=2, ensure_ascii=False)
        logger.info("Resumen MirSpiro simulado: %d items desde PDFs", len(items_scan))
    else:
        # ── Flujo completo: Módulo 1 + 2 ──
        pacientes = modulo_1(logger, fecha_inicio, fecha_fin)
        resultados_mirspiro = modulo_2(logger, pacientes, fecha_fallback=fecha_inicio)

        resumen_path = Path(config.DATA_DIR) / "resultados_mirspiro.json"
        with open(resumen_path, "w", encoding="utf-8") as f:
            json.dump(resultados_mirspiro, f, indent=2, ensure_ascii=False)
        logger.info("Resumen MirSpiro guardado en %s", resumen_path)

    # ── Módulo 3 (compartido) ──
    resultados_sunu = modulo_3(logger, fecha_objetivo=fecha_inicio)

    resumen_final_path = Path(config.DATA_DIR) / "resultados_final.json"
    with open(resumen_final_path, "w", encoding="utf-8") as f:
        json.dump({
            "mirspiro": resultados_mirspiro,
            "sunu": resultados_sunu,
            "fecha_inicio": fecha_inicio.isoformat(),
            "fecha_fin": fecha_fin.isoformat(),
            "sede": config.SEDE_LOCAL,
        }, f, indent=2, ensure_ascii=False)
    logger.info("Resumen final guardado en %s", resumen_final_path)

    _generar_reporte_local(
        logger, config.SEDE_LOCAL, etiqueta_fecha,
        resultados_mirspiro, resultados_sunu,
    )

    alerta = resultados_mirspiro.get("abortado_temprano") or resultados_sunu.get("abortado_temprano")
    _enviar_reporte_email(
        logger, config.SEDE_LOCAL, etiqueta_fecha,
        resultados_mirspiro, resultados_sunu,
        alerta=alerta,
    )

    logger.info("=== FIN ===")


def _items_desde_pdfs(logger, fecha_fallback: date | None = None) -> list[dict]:
    """Escanea la carpeta de PDFs y construye items (cedula, fecha) desde los nombres de archivo.

    Si se proporciona fecha_fallback, se usa para TODOS los PDFs
    (ignorando cualquier fecha incrustada en el nombre).

    Si no, extrae la fecha del formato {cedula}_{YYYY-MM-DD}.pdf;
    los PDFs sin fecha en el nombre se omiten.

    Returns:
        lista de dicts con "cedula" y "fecha" (string ISO).
    """
    carpeta = Path(config.PDF_DIR)
    if not carpeta.is_dir():
        logger.error("La carpeta de PDFs no existe: %s", carpeta)
        return []

    items: list[dict] = []

    for pdf in sorted(carpeta.glob("*.pdf")):
        stem = pdf.stem

        if fecha_fallback is not None:
            # Forzar la misma fecha para todos (ignorar nombre)
            items.append({"cedula": stem, "fecha": fecha_fallback.isoformat()})
            continue

        # Sin fallback: extraer fecha del nombre {cedula}_{YYYY-MM-DD}.pdf
        if "_" in stem:
            partes = stem.rsplit("_", 1)
            try:
                fecha_item = date.fromisoformat(partes[1])
                items.append({"cedula": partes[0], "fecha": fecha_item.isoformat()})
                continue
            except (ValueError, IndexError):
                pass

        # Sin fallback y sin fecha en nombre → se omite
        logger.debug("PDF sin fecha en nombre (omitido): %s", pdf.name)

    if not items:
        logger.warning(
            "No se encontraron PDFs procesables en %s%s",
            carpeta,
            ". Usa --solo-subir FECHA si los PDFs no tienen fecha en el nombre."
            if fecha_fallback is None else "",
        )
        return []

    logger.info(
        "Escaneados %d PDFs desde %s", len(items), config.PDF_DIR,
    )
    return items


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
