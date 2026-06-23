import os
from dotenv import load_dotenv

load_dotenv()

URL_NUBE = os.getenv("URL_NUBE", "")
URL_REPORTE = os.getenv("URL_REPORTE", "")
USUARIO = os.getenv("USUARIO", "")
PASSWORD = os.getenv("PASSWORD", "")
SEDE_LOCAL = os.getenv("SEDE_LOCAL", "").upper()
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "downloads")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, DOWNLOAD_PATH)
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "bot.log")
PACIENTES_FILE = os.path.join(DATA_DIR, "pacientes_pendientes.json")

# ── MirSpiro ──────────────────────────────────────────────
MIRSPIRO_EXE = os.getenv("MIRSPIRO_EXE", "")
MIRSPIRO_TYPING_DELAY = float(os.getenv("MIRSPIRO_TYPING_DELAY", "0.05"))
PDF_DIR = os.path.join(BASE_DIR, SEDE_LOCAL.lower(), "pdfs")

SELECTORS = {
    "username_input": os.getenv("SEL_USERNAME", "input[name='email']"),
    "password_input": os.getenv("SEL_PASSWORD", "input[name='password']"),
    "login_button": os.getenv("SEL_LOGIN_BTN", "button:has-text('Ingresar')"),
    "fecha_inicio": os.getenv("SEL_FECHA_INICIO", "#fecha_inicio"),
    "fecha_fin": os.getenv("SEL_FECHA_FIN", "#fecha_fin"),
    "filtros": os.getenv("SEL_FILTROS", "#filtros"),
    "enviar_button": os.getenv("SEL_ENVIAR", "button.btnSubmitReportes"),
    "download_button": os.getenv("SEL_DOWNLOAD", "button:has-text('Descargar')"),
}
