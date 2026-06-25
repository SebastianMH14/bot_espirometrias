# Bot Espirometrías — RPA

Automatización de 3 pasos para espirometrías en CEMDE:

1. **Descargar** reporte Excel desde Sunu con pacientes del día anterior
2. **Generar** PDF de cada paciente en MirSpiro (app de escritorio Windows)
3. **Subir** cada PDF al perfil del paciente en Sunu (web)

---

## Requisitos

- **Windows** (MirSpiro es una app WinForms)
- **Python 3.10+**
- MirSpiro instalado (ruta configurable en `.env`)
- Chrome (para Selenium)

## Instalación

```bash
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # editar con credenciales reales
```

## Configuración (`.env`)

| Variable | Descripción |
|---|---|
| `URL_NUBE` | Base URL de Sunu (ej: `https://cemde.sunu.be`) |
| `URL_REPORTE` | URL del reporte de atenciones |
| `USUARIO` / `PASSWORD` | Credenciales Sunu |
| `SEDE_LOCAL` | Una de: `BELLO`, `SABANETA`, `LAURELES`, `RIO NEGRO` |
| `MIRSPIRO_EXE` | Ruta completa al ejecutable de MirSpiro |
| `MIRSPIRO_TYPING_DELAY` | Pausa entre caracteres al escribir (por defecto `0.05`) |

> Cada sede ejecuta su propia instancia del bot con `SEDE_LOCAL` distinta.

## Ejecución

```bash
python main.py
```

El pipeline completo corre en este orden:

1. **Módulo 1** — Inicia sesión en Sunu, filtra reporte por fecha y servicio `ESPIROMETRIA CEMDE`, descarga el Excel, lo parsea y filtra por sede. Guarda los pacientes en `data/pacientes_pendientes.json`.
2. **Módulo 2** — Conecta a MirSpiro por UI Automation (uiautomation), busca cada paciente por cédula, imprime a PDF en `<sede>/<cedula>.pdf`. Por cada fallo guarda captura de pantalla + volcado UIA en `debug/`.
3. **Módulo 3** — Inicia sesión en Sunu de nuevo, navega al perfil de cada paciente, localiza la fila de espirometría del día anterior y adjunta el PDF.

## Output

- `data/resultados_mirspiro.json` — resumen de PDFs generados
- `data/resultados_final.json` — resumen combinado (MirSpiro + Sunu)
- `logs/bot_YYYYMMDD_HHMMSS.txt` — log detallado de la ejecución
- `debug/` — screenshots + HTML/UIA tree de fallos por paciente

## Estructura

```
├── config.py            # Config desde .env
├── main.py              # Entry point, orquesta los 3 módulos
├── modules/
│   ├── nube.py          # Módulo 1 — Selenium + login, descarga Excel
│   ├── excel.py         # Parseo y filtrado del Excel
│   ├── mirspiro_module.py # Módulo 2 — RPA escritorio (uiautomation)
│   ├── subir_sunu.py    # Módulo 3 — subida PDF a Sunu (Selenium)
│   └── logger.py        # Logger con archivo rotado + consola
├── explorar_mirspiro.py   # Diagnóstico: vuelca árbol UIA de MirSpiro
├── explorar_guardar_como.py # Diagnóstico: inspecciona diálogo Guardar como
├── diagnostico_modal.py    # Diagnóstico: bounds de modal de impresión
└── mouse_pos.py            # Utilidad: muestra coordenadas del mouse
```

## Debugging

- Cada fallo en Módulo 2 genera: `<cedula>_error.png` + `<cedula>_uia_dump.txt`
- Cada fallo en Módulo 3 genera: `<cedula>_error.png` + `<cedula>_page.html`
- Los scripts en `explorar_*.py` permiten inspeccionar la UI de MirSpiro sin ejecutar el pipeline completo.
