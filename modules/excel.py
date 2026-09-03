import json
import logging
import pandas as pd

import config

logger = logging.getLogger("bot_espirometrias")

COL_VARIANTS = {
    "cedula": ["cedula", "cédula", "documento", "identificación", "identificacion", "id", "numero documento", "nro documento", "identificacion del paciente"],
    "sede": ["sede", "centro", "lugar", "ubicacion", "ubicación", "sucursal", "sede cemde"],
    "fecha": ["fecha", "fecha de atencion", "fecha atención", "fecha de realizacion",
              "fecha realizacion", "fecha de la cita", "fecha cita", "fecha de atencion",
              "fecha_atencion", "fecha de realizacion del procedimiento",
              "fecha atencion", "fecha_atencion_", "fec atencion", "fec. atención",
              "fecha de atención", "fecha atención", "fec. atencion", "fecha cita",
              "fechaatencion", "fecharealizacion", "fecha realizacion procedimiento",
              "fecha de cita"],
}


_TILDES = str.maketrans("áéíóúñ", "aeioun")


def _sin_tildes(s: str) -> str:
    return s.translate(_TILDES)


def _normalizar_columnas(df):
    columnas_normalizadas = {
        c: _sin_tildes(c.lower().strip().replace(" ", "")) for c in df.columns
    }
    col_cedula = None
    col_sede = None
    col_fecha = None

    for col_original, col_lower in columnas_normalizadas.items():
        if col_cedula is None:
            for variant in COL_VARIANTS["cedula"]:
                v = _sin_tildes(variant.lower().strip().replace(" ", ""))
                if v == col_lower:
                    col_cedula = col_original
                    break
        if col_sede is None:
            for variant in COL_VARIANTS["sede"]:
                v = _sin_tildes(variant.lower().strip().replace(" ", ""))
                if v == col_lower:
                    col_sede = col_original
                    break
        if col_fecha is None:
            for variant in COL_VARIANTS["fecha"]:
                v = _sin_tildes(variant.lower().strip().replace(" ", ""))
                if v == col_lower:
                    col_fecha = col_original
                    break
        # Nota: antes se cortaba el bucle apenas se encontraban cédula+sede,
        # lo que dejaba sin revisar la columna de fecha si venía después en
        # el Excel (causa probable de por qué "fecha" salía None en varios
        # reportes reales). Ahora se recorren todas las columnas siempre.
        if col_cedula and col_sede and col_fecha:
            break

    if not col_cedula:
        raise ValueError(
            f"No se encontró columna de cédula. Columnas disponibles: {list(df.columns)}"
        )
    if not col_sede:
        raise ValueError(
            f"No se encontró columna de sede. Columnas disponibles: {list(df.columns)}"
        )

    rename_map = {col_cedula: "cedula", col_sede: "sede"}
    if col_fecha:
        rename_map[col_fecha] = "fecha"
    df = df.rename(columns=rename_map)
    logger.debug(
        "Columnas detectadas: cédula -> '%s', sede -> '%s'%s",
        col_cedula, col_sede,
        f", fecha -> '{col_fecha}'" if col_fecha else " (sin columna fecha)",
    )
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

    if "fecha" in df.columns:
        df["fecha"] = pd.to_datetime(df["fecha"], dayfirst=True, errors="coerce").dt.date
    else:
        df["fecha"] = None

    antes = len(df)
    df = df.dropna(subset=["cedula", "sede"])
    df = df[df["cedula"] != ""]
    df = df[df["sede"] != ""]
    subset_dedup = ["cedula", "fecha"] if "fecha" in df.columns and df["fecha"].notna().any() else ["cedula"]
    df = df.drop_duplicates(subset=subset_dedup)
    logger.debug("Registros: %d -> %d (limpios, dedup por %s)", antes, len(df), subset_dedup)

    cols_out = ["cedula", "sede"]
    if "fecha" in df.columns:
        cols_out.append("fecha")
    pacientes = df[cols_out].to_dict(orient="records")
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
