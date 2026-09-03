"""Tests unitarios de modules/excel.py — parseo y filtrado del reporte Sunu.

No dependen de Selenium ni de un Excel real de Sunu: usan DataFrames /
archivos .xlsx temporales construidos en memoria, así corren en cualquier
máquina (incluida CI) sin credenciales ni navegador.
"""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from modules.excel import _normalizar_columnas, filtrar_por_sede, leer_excel


class TestNormalizarColumnas(unittest.TestCase):
    def test_detecta_variantes_de_nombre(self):
        df = pd.DataFrame({
            "Número Documento": ["123"],
            "Sede CEMDE": ["LAURELES"],
            "Fecha de Atención": ["2026-01-01"],
        })
        resultado = _normalizar_columnas(df)
        self.assertIn("cedula", resultado.columns)
        self.assertIn("sede", resultado.columns)
        self.assertIn("fecha", resultado.columns)

    def test_sin_columna_fecha_no_falla(self):
        df = pd.DataFrame({"Cedula": ["123"], "Sede": ["LAURELES"]})
        resultado = _normalizar_columnas(df)
        self.assertIn("cedula", resultado.columns)
        self.assertIn("sede", resultado.columns)
        self.assertNotIn("fecha", resultado.columns)

    def test_sin_columna_cedula_lanza_valueerror(self):
        df = pd.DataFrame({"Sede": ["LAURELES"], "Otro": [1]})
        with self.assertRaises(ValueError):
            _normalizar_columnas(df)

    def test_sin_columna_sede_lanza_valueerror(self):
        df = pd.DataFrame({"Cedula": ["123"], "Otro": [1]})
        with self.assertRaises(ValueError):
            _normalizar_columnas(df)


class TestFiltrarPorSede(unittest.TestCase):
    def test_filtra_case_insensitive(self):
        pacientes = [
            {"cedula": "1", "sede": "LAURELES, ANTIOQUIA"},
            {"cedula": "2", "sede": "BELLO"},
            {"cedula": "3", "sede": "laureles, antioquia"},
        ]
        filtrados = filtrar_por_sede(pacientes, "laureles, antioquia")
        self.assertEqual({"1", "3"}, {p["cedula"] for p in filtrados})

    def test_sin_coincidencias_retorna_lista_vacia(self):
        pacientes = [{"cedula": "1", "sede": "BELLO"}]
        self.assertEqual([], filtrar_por_sede(pacientes, "RIO NEGRO"))


class TestLeerExcel(unittest.TestCase):
    def _escribir_excel(self, df: pd.DataFrame) -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        tmp.close()
        df.to_excel(tmp.name, index=False, engine="openpyxl")
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_lee_filtra_y_deduplica(self):
        df = pd.DataFrame({
            "Cedula": ["123", "123", " 456 ", ""],
            "Sede": ["laureles", "laureles", "bello", "laureles"],
            "Fecha": ["01/09/2026", "01/09/2026", "02/09/2026", "03/09/2026"],
        })
        ruta = self._escribir_excel(df)
        pacientes = leer_excel(ruta)

        # La fila con cédula vacía se descarta
        self.assertTrue(all(p["cedula"] for p in pacientes))
        # El duplicado exacto (misma cédula + misma fecha) se elimina
        cedulas = [p["cedula"] for p in pacientes]
        self.assertEqual(cedulas.count("123"), 1)
        # La sede queda en mayúsculas
        self.assertTrue(all(p["sede"] == p["sede"].upper() for p in pacientes))

    def test_sin_columna_fecha_no_rompe(self):
        df = pd.DataFrame({"Cedula": ["789"], "Sede": ["bello"]})
        ruta = self._escribir_excel(df)
        pacientes = leer_excel(ruta)
        self.assertEqual(1, len(pacientes))
        self.assertEqual("789", pacientes[0]["cedula"])


if __name__ == "__main__":
    unittest.main()
