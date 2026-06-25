import json
import logging
import pandas as pd

import config

logger = logging.getLogger("bot_espirometrias")

COL_VARIANTS = {
    "cedula": ["cedula", "cédula", "documento", "identificación", "identificacion", "id", "numero documento", "nro documento", "identificacion del paciente"],
    "sede": ["sede", "centro", "lugar", "ubicacion", "ubicación", "sucursal", "sede cemde"],
}


def _normalizar_columnas(df):
    columnas_normalizadas = {c: c.lower().strip().replace(" ", "") for c in df.columns}
    col_cedula = None
    col_sede = None

    for col_original, col_lower in columnas_normalizadas.items():
        if col_cedula is None:
            for variant in COL_VARIANTS["cedula"]:
                v = variant.lower().strip().replace(" ", "").replace("ó", "o")
                cl = col_lower.replace("ó", "o")
                if v == cl:
                    col_cedula = col_original
                    break
        if col_sede is None:
            for variant in COL_VARIANTS["sede"]:
                v = variant.lower().strip().replace(" ", "").replace("ó", "o")
                cl = col_lower.replace("ó", "o")
                if v == cl:
                    col_sede = col_original
                    break
        if col_cedula and col_sede:
            break

    if not col_cedula:
        raise ValueError(
            f"No se encontró columna de cédula. Columnas disponibles: {list(df.columns)}"
        )
    if not col_sede:
        raise ValueError(
            f"No se encontró columna de sede. Columnas disponibles: {list(df.columns)}"
        )

    df = df.rename(columns={col_cedula: "cedula", col_sede: "sede"})
    logger.debug("Columnas detectadas: cédula -> '%s', sede -> '%s'", col_cedula, col_sede)
    return df


def leer_excel(ruta_excel):
    logger.info("Leyendo Excel: %s", ruta_excel)
    for engine in ["calamine", "openpyxl"]:
        try:
            df = pd.read_excel(ruta_excel, engine=engine, dtype=str)
            break
        except Exception as e:
            logger.warning("Engine %s falló: %s", engine, e)
    else:
        raise ValueError(f"No se pudo leer el Excel: {ruta_excel}")
    logger.debug("Excel cargado con %d filas y %d columnas", df.shape[0], df.shape[1])

    df = _normalizar_columnas(df)

    df["cedula"] = df["cedula"].astype(str).str.strip()
    df["sede"] = df["sede"].astype(str).str.strip().str.upper()

    antes = len(df)
    df = df.dropna(subset=["cedula", "sede"])
    df = df[df["cedula"] != ""]
    df = df[df["sede"] != ""]
    df = df.drop_duplicates(subset=["cedula"])
    logger.debug("Registros: %d -> %d (limpios)", antes, len(df))

    pacientes = df[["cedula", "sede"]].to_dict(orient="records")
    logger.info("%d pacientes encontrados en el Excel", len(pacientes))
    return pacientes


def filtrar_por_sede(pacientes, sede_local):
    sede_local = sede_local.strip().upper()
    filtrados = [p for p in pacientes if p["sede"].upper() == sede_local]
    logger.info(
        "%d pacientes filtrados para %s (de %d totales)",
        len(filtrados),
        sede_local,
        len(pacientes),
    )
    return filtrados


def guardar_pacientes(pacientes):
    data = [
        {
            "cedula": p["cedula"],
            "sede": p["sede"],
            "estado": "PENDIENTE",
        }
        for p in pacientes
    ]

    with open(config.PACIENTES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info("Pacientes guardados: %s (%d registros)", config.PACIENTES_FILE, len(data))
    return config.PACIENTES_FILE
