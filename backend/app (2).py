from flask import Flask, request, jsonify, session, send_from_directory, redirect, url_for, Response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
import random
import json
import os
import re
import time
from sqlalchemy import func, event, or_
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, DisconnectionError
from sqlalchemy.pool import NullPool

try:
    from backend.models import db, Usuario, SesionExperimental, ResultadoIdentificacion, ResultadoEncuesta, ReflexionMetacognitiva, Configuracion, PasoClave
except ImportError:
    from models import db, Usuario, SesionExperimental, ResultadoIdentificacion, ResultadoEncuesta, ReflexionMetacognitiva, Configuracion, PasoClave

# Módulo de estadística (backend/estadistica.py). Si falta, la plataforma funciona igual y solo el análisis estadístico avisa que falta subirlo.
try:
    from backend import estadistica
except ImportError:
    try:
        import estadistica
    except ImportError:
        estadistica = None

import datetime as _dt

# ===== CONFIGURACIÓN =====
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, '..', 'frontend')

app = Flask(
    __name__,
    static_folder=os.path.join(FRONTEND_DIR, 'static'),
    template_folder=os.path.join(FRONTEND_DIR, 'templates')
)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
if not app.config['SECRET_KEY']:
    raise RuntimeError('SECRET_KEY no está definida en las variables de entorno')

# Base de datos: PostgreSQL en Render, SQLite local
database_url = os.getenv('DATABASE_URL', 'sqlite:///database.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Conexión a PostgreSQL. Render y los proveedores en la nube cierran las conexiones inactivas o las cortan sin avisar:
# se comprueban antes de usarlas (pre_ping), se renuevan cada ~5 minutos, se avisa al servidor que seguimos vivos
# (keepalives) y se limita la espera para conectar. Evita "SSL error: decryption failed or bad record mac",
# "SSL SYSCALL error: EOF detected" y "error with status PGRES_TUPLES_OK and no message from the libpq".
def opciones_motor(url):
    if not url.startswith('postgresql'):
        return {}                                   # SQLite (pruebas locales): sin opciones especiales
    conexion = {'connect_timeout': 15, 'keepalives': 1, 'keepalives_idle': 30, 'keepalives_interval': 10, 'keepalives_count': 5}
    # Neon "pooled" (-pooler) ya reutiliza las conexiones en el servidor (PgBouncer): se abre una conexión por petición
    # y no se guarda un segundo pool encima, que es una fuente conocida de conexiones dañadas.
    if '-pooler' in url.split('@')[-1].split('/')[0]:
        return {'poolclass': NullPool, 'connect_args': conexion}
    # pre_ping DESACTIVADO a propósito: el de SQLAlchemy solo reconoce algunos textos de error y, con otros (p. ej. la redacción de
    # OpenSSL 3 "ssl/tls alert bad record mac"), falla en vez de abrir otra conexión. Lo reemplaza comprobar_conexion_al_prestarla().
    # Neon pausa el cómputo tras 5 minutos sin uso y corta las conexiones: se renuevan cada 4 minutos y el pool es pequeño.
    return {'pool_pre_ping': False, 'pool_recycle': 240, 'pool_size': 3, 'max_overflow': 3, 'connect_args': conexion}

app.config['SQLALCHEMY_ENGINE_OPTIONS'] = opciones_motor(database_url)
MODO_POOL = 'sin pool propio (Neon pooler)' if 'poolclass' in app.config['SQLALCHEMY_ENGINE_OPTIONS'] else ('pool con comprobación' if database_url.startswith('postgresql') else 'SQLite')
print(f'[bd] Conexión a la base: {MODO_POOL}', flush=True)

# SQLAlchemy solo descarta una conexión dañada si reconoce el mensaje de error. Estos mensajes de libpq/psycopg2 no estaban
# en su lista: la conexión rota volvía al pool y las peticiones siguientes fallaban igual hasta reiniciar el servicio.
MENSAJES_CONEXION_ROTA = ('no message from the libpq', 'PGRES_TUPLES_OK', 'bad record mac', 'SSL SYSCALL', 'EOF detected',
                          'server closed the connection', 'connection already closed', 'terminating connection',
                          'could not receive data', 'could not send data', 'connection not open', 'connection has been closed')

@event.listens_for(Engine, 'do_connect')
def conectar_con_reintentos(dialect, registro, cargs, cparams):
    """Neon pausa el cómputo tras 5 minutos sin uso y tarda un instante en despertarlo: la primera conexión puede fallar con un error
    de TLS aunque la base esté bien. Se reintenta hasta 3 veces (con una pausa creciente) antes de rendirse."""
    if dialect.name != 'postgresql':
        return None
    ultimo = None
    for intento in range(3):
        try:
            return dialect.loaded_dbapi.connect(*cargs, **cparams)
        except dialect.loaded_dbapi.OperationalError as e:
            ultimo = e
            print(f'[bd] No se pudo abrir la conexión (intento {intento + 1} de 3): {str(e).splitlines()[0][:120]}', flush=True)
            if intento < 2:
                time.sleep(0.5 * (intento + 1))
    raise ultimo

try:
    import psycopg2.extensions as _pg_ext
except ImportError:          # SQLite en pruebas locales
    _pg_ext = None

@event.listens_for(Engine, 'checkout')
def comprobar_conexion_al_prestarla(dbapi_con, registro, proxy):
    """Antes de usar una conexión del pool se hace un SELECT 1. Si falla por CUALQUIER motivo, la conexión no sirve (la base se
    pausó, la red la cortó, TLS se rompió...): se lanza DisconnectionError y SQLAlchemy descarta esa conexión y abre otra, hasta 3
    veces, sin que el estudiante ni el administrador vean un error. No depende del texto del mensaje, que cambia según la versión
    de OpenSSL ("decryption failed or bad record mac" / "ssl/tls alert bad record mac")."""
    if _pg_ext is None or not isinstance(dbapi_con, _pg_ext.connection):
        return
    cursor = None
    try:
        cursor = dbapi_con.cursor()
        cursor.execute('SELECT 1')
        dbapi_con.rollback()
    except Exception:
        raise DisconnectionError('conexión inservible: se abre una nueva')
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass

@event.listens_for(Engine, 'handle_error')
def descartar_conexion_rota(contexto):
    mensaje = str(contexto.original_exception or '')
    if any(m in mensaje for m in MENSAJES_CONEXION_ROTA):
        contexto.is_disconnect = True
        fase = 'sondeo previo' if getattr(contexto, 'is_pre_ping', False) else ('al conectar' if contexto.connection is None else 'durante una consulta')
        print(f'[bd] Conexión descartada ({fase}): {mensaje.splitlines()[0][:120]}', flush=True)

CORS(app, supports_credentials=True, origins=os.getenv('ALLOWED_ORIGIN', 'http://127.0.0.1:5000'))

@app.errorhandler(DBAPIError)
def error_de_base_de_datos(e):
    """Una interrupción momentánea de la base no debe verse como un error de código: se limpia la sesión y se pide reintentar."""
    try:
        db.session.rollback()
    except Exception:
        pass
    print(f'[bd] Error de base de datos en {request.method} {request.path}: {str(e).splitlines()[0][:200]}', flush=True)
    mensaje = 'La base de datos tuvo una interrupción momentánea. Intenta de nuevo en unos segundos.'
    if request.path.startswith('/api/'):
        respuesta = jsonify({'error': mensaje, 'codigo': 'bd_temporal'})
        respuesta.status_code = 503
        respuesta.headers['X-BD-Temporal'] = '1'
        return respuesta
    return mensaje, 503

class ReintentarApiGetBD:
    """Si una petición GET de /api/ falla por una interrupción momentánea de la base (error marcado como temporal), se repite UNA vez
    de forma automática: con la conexión ya renovada casi siempre funciona, y el usuario no ve ningún error. Solo GET (no cambian
    datos), nunca POST: los guardados los reintenta la pantalla del estudiante."""
    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        if environ.get('REQUEST_METHOD') != 'GET' or not environ.get('PATH_INFO', '').startswith('/api/'):
            return self.wsgi(environ, start_response)
        for intento in (1, 2):
            captura = {}
            def sr(status, headers, exc_info=None):
                captura['status'], captura['headers'] = status, headers
                return lambda data: None
            iterador = self.wsgi(environ, sr)
            try:
                cuerpo = b''.join(iterador)
            finally:
                if hasattr(iterador, 'close'):
                    iterador.close()
            temporal = captura['status'].startswith('503') and any(k.lower() == 'x-bd-temporal' for k, _ in captura['headers'])
            if temporal and intento == 1:
                print(f"[bd] Se repite automáticamente GET {environ.get('PATH_INFO', '')}", flush=True)
                continue
            break
        start_response(captura['status'], captura['headers'])
        return [cuerpo]

app.wsgi_app = ReintentarApiGetBD(app.wsgi_app)

# ===== ESQUEMA: agrega columnas nuevas a una base creada con una versión anterior =====
# db.create_all() solo crea tablas que NO existen; no agrega columnas a las que ya existen. Por eso, si la base se
# creó (o se restauró de un respaldo) con una versión vieja, esta función completa las columnas que falten.
COLUMNAS_NUEVAS = {
    'usuarios': [('conocimiento_genero', 'INTEGER DEFAULT 1'), ('puntaje_rotacion_mental', 'INTEGER'), ('institucion', 'VARCHAR(80)'), ('rotacion_items', 'TEXT'), ('sesion_aplicacion', 'INTEGER')],
    'resultados': [('tiempo_reflexion_segundos', 'DOUBLE PRECISION'), ('primera_pregunta_desvio', 'INTEGER'),
                   ('ayuda_encuadre_usos', 'INTEGER DEFAULT 0'), ('ayuda_encuadre_detalle', 'TEXT')],
    'reflexiones': [('orden_especimen', 'INTEGER')],
'sesiones': [('anulada', 'BOOLEAN DEFAULT FALSE'), ('motivo_anulacion', 'TEXT'), ('fecha_anulacion', 'TIMESTAMP'), ('anulada_por', 'VARCHAR(80)')],
'pasos_clave': [('deshecho', 'BOOLEAN DEFAULT FALSE')],
}

def asegurar_esquema():
    """Completa las columnas que falten. Devuelve (columnas_agregadas, errores)."""
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    es_postgres = db.engine.dialect.name == 'postgresql'
    agregadas, errores = [], []
    for tabla, columnas in COLUMNAS_NUEVAS.items():
        if not insp.has_table(tabla):
            continue
        existentes = {c['name'] for c in insp.get_columns(tabla)}
        for nombre, tipo in columnas:
            if nombre in existentes:
                continue
            try:
                si_no_existe = 'IF NOT EXISTS ' if es_postgres else ''   # evita choques si 2 workers lo hacen a la vez
                db.session.execute(text(f'ALTER TABLE {tabla} ADD COLUMN {si_no_existe}{nombre} {tipo}'))
                db.session.commit()
                agregadas.append(f'{tabla}.{nombre}')
            except Exception as e:
                db.session.rollback()
                errores.append(f'{tabla}.{nombre}: {str(e).splitlines()[0][:200]}')
    # Los usuarios de versiones anteriores quedan como estudiantes
    try:
        if insp.has_table('usuarios'):
            db.session.execute(text("UPDATE usuarios SET rol = 'estudiante' WHERE rol IS NULL OR rol = 'usuario'"))
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        errores.append(f'usuarios.rol: {str(e).splitlines()[0][:200]}')
    if agregadas:
        print('[esquema] Columnas agregadas: ' + ', '.join(agregadas), flush=True)
    if errores:
        print('[esquema] NO se pudieron agregar: ' + ' | '.join(errores), flush=True)
    return agregadas, errores

db.init_app(app)
with app.app_context():
    try:
        db.create_all()
    except Exception:
        # En una base vacía, los 2 workers de gunicorn pueden crear las tablas a la vez: se reintenta una vez
        db.session.rollback()
        import time
        time.sleep(2)
        db.create_all()
    try:
        asegurar_esquema()
        print('[esquema] Esquema verificado al arrancar', flush=True)
    except Exception as e:
        db.session.rollback()
        print(f'[esquema] No se pudo verificar el esquema: {e}', flush=True)

# ===== CREDENCIALES ADMIN (solo desde variables de entorno) =====
ADMIN_USER = os.environ.get('ADMIN_USER')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
if not ADMIN_USER or not ADMIN_PASSWORD:
    raise RuntimeError('ADMIN_USER y ADMIN_PASSWORD deben estar definidos en las variables de entorno')

# ===== POOL DE ESPECIES =====
POOL_ESPECIES = [
    {'id': 'humile',      'nombre': 'Linepithema humile',      'activa': True},
    {'id': 'angulatum',   'nombre': 'Linepithema angulatum',   'activa': True},
    {'id': 'piliferum',   'nombre': 'Linepithema piliferum',   'activa': True},
    {'id': 'gallardoi',   'nombre': 'Linepithema gallardoi',   'activa': True},
    {'id': 'iniquum',     'nombre': 'Linepithema iniquum',     'activa': True},
    {'id': 'neotropicum', 'nombre': 'Linepithema neotropicum', 'activa': True},
    {'id': 'hirsutum',    'nombre': 'Linepithema hirsutum',    'activa': True},
    {'id': 'dispertitum', 'nombre': 'Linepithema dispertitum', 'activa': True},
    {'id': 'tsachila',    'nombre': 'Linepithema tsachila',    'activa': True},
]

# ===== DECORADORES =====
def login_required(f):
    """Protege rutas que requieren sesión de estudiante."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('usuario_id'):
            return redirect(url_for('serve_login'))
        return f(*args, **kwargs)
    return decorated

# ===== SESIÓN DE UNA CUENTA QUE YA NO EXISTE =====
# Si el navegador conserva la sesión de un usuario que fue borrado (o la base de datos cambió), antes el panel se abría a medias, con errores 404
# «Usuario no encontrado». Ahora esa sesión se cierra: las rutas responden «no autenticado» y el panel manda al inicio de sesión.
# Solo se comprueba en las rutas de consulta de la propia cuenta (una lectura por clave primaria); no en los guardados frecuentes durante el ejercicio.
RUTAS_QUE_VERIFICAN_LA_CUENTA = ('/api/usuario/', '/api/verificar_sesion', '/api/experimento/estado', '/api/experimento/iniciar')

@app.before_request
def cerrar_sesion_si_la_cuenta_ya_no_existe():
    uid = session.get('usuario_id')
    if uid is None or not request.path.startswith(RUTAS_QUE_VERIFICAN_LA_CUENTA):
        return None
    try:
        existe = db.session.get(Usuario, uid) is not None
    except Exception:
        # Un fallo momentáneo de la base NO debe expulsar a nadie: se deja pasar y la ruta resolverá (o fallará) por su cuenta.
        db.session.rollback()
        return None
    if not existe:
        session.pop('usuario_id', None)
        session.pop('usuario_nombre', None)
    return None

def admin_required(f):
    """Protege rutas que requieren sesión de administrador."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return jsonify({'error': 'Acceso no autorizado'}), 401
        return f(*args, **kwargs)
    return decorated

# ===== UTILIDADES =====
def calcular_sus(respuestas):
    """
    Calcula el puntaje SUS a partir de una lista de 10 respuestas (1-5)
    o un diccionario {clave: valor}.
    Retorna un float entre 0 y 100, o None si los datos son inválidos.
    """
    try:
        if isinstance(respuestas, dict):
            valores = [int(v) for v in respuestas.values() if str(v).isdigit()]
        elif isinstance(respuestas, list):
            valores = [int(v) for v in respuestas]
        else:
            return None

        if len(valores) != 10:
            return None

        score = 0
        for i in range(10):
            if i % 2 == 0:
                score += valores[i] - 1
            else:
                score += 5 - valores[i]
        return round(max(0.0, min(100.0, score * 2.5)), 2)
    except (ValueError, TypeError):
        return None

def get_especies_activas():
    """Especies que se sortean. Si la configuración guardada tiene menos de 2 especies válidas (cada participante
    necesita 2 distintas), se ignora y se usan las activas por defecto, para que el ejercicio nunca quede bloqueado."""
    por_defecto = [e['id'] for e in POOL_ESPECIES if e['activa']]
    config = Configuracion.query.filter_by(clave='especies_activas').first()
    if config:
        try:
            validas = [i for i in json.loads(config.valor) if i in {e['id'] for e in POOL_ESPECIES}]
        except Exception:
            validas = []
        if len(validas) >= 2:
            return validas
        print('[especies] Configuración guardada con menos de 2 especies válidas: se usan las de por defecto', flush=True)
    return por_defecto

def set_especies_activas(especies_ids):
    config = Configuracion.query.filter_by(clave='especies_activas').first()
    if config:
        config.valor = json.dumps(especies_ids)
    else:
        config = Configuracion(clave='especies_activas', valor=json.dumps(especies_ids))
        db.session.add(config)
    db.session.commit()

def get_pool_especimenes():
    especies_activas = get_especies_activas()
    pool = []
    for especie in especies_activas:
        pool.append({'id': f'{especie}_1', 'especie': especie})
        pool.append({'id': f'{especie}_2', 'especie': especie})
    return pool

def get_posiciones_pines():
    """Devuelve las posiciones (px, py en %) de los pines del glosario
    interactivo, ajustadas por el admin. Vacío si nunca se han guardado
    (el frontend usa entonces sus coordenadas por defecto)."""
    config = Configuracion.query.filter_by(clave='posiciones_pines').first()
    if config:
        return json.loads(config.valor)
    return {}

def set_posiciones_pines(posiciones):
    config = Configuracion.query.filter_by(clave='posiciones_pines').first()
    if config:
        config.valor = json.dumps(posiciones)
    else:
        config = Configuracion(clave='posiciones_pines', valor=json.dumps(posiciones))
        db.session.add(config)
    db.session.commit()

def get_zoom_caracteres():
    """Devuelve la configuración de encuadre por pregunta de la clave:
    para 2D, a qué zona de la foto hacer zoom (x%, y%, escala); para 3D,
    hacia dónde orientar la cámara del modelo (camera-orbit, camera-target,
    field-of-view). Vacío si el admin nunca lo ha calibrado."""
    config = Configuracion.query.filter_by(clave='zoom_caracteres').first()
    if config:
        return json.loads(config.valor)
    return {}

def set_zoom_caracteres(datos):
    config = Configuracion.query.filter_by(clave='zoom_caracteres').first()
    if config:
        config.valor = json.dumps(datos)
    else:
        config = Configuracion(clave='zoom_caracteres', valor=json.dumps(datos))
        db.session.add(config)
    db.session.commit()

# ===== RUTAS HTML =====
@app.route('/')
def index():
    return send_from_directory(app.template_folder, 'login.html')

@app.route('/login')
def serve_login():
    return send_from_directory(app.template_folder, 'login.html')

@app.route('/registro')
def serve_registro():
    return send_from_directory(app.template_folder, 'registro.html')

@app.route('/dashboard')
@login_required
def serve_dashboard():
    return send_from_directory(app.template_folder, 'dashboard.html')

@app.route('/clave_2d')
@login_required
def serve_clave_2d():
    return send_from_directory(app.template_folder, 'clave_2d.html')

@app.route('/clave_2d_meta')
@login_required
def serve_clave_2d_meta():
    return send_from_directory(app.template_folder, 'clave_2d_meta.html')

@app.route('/clave_3d')
@login_required
def serve_clave_3d():
    return send_from_directory(app.template_folder, 'clave_3d.html')

@app.route('/clave_3d_meta')
@login_required
def serve_clave_3d_meta():
    return send_from_directory(app.template_folder, 'clave_3d_meta.html')

@app.route('/estadisticas')
def estadisticas():
    return send_from_directory(app.template_folder, 'estadisticas.html')

@app.route('/prueba-niveles-ayuda')
def prueba_niveles_ayuda():
    """Herramienta de prueba interna: NO guarda nada en la base de datos,
    es solo para que el admin sienta cómo se ve identificar con distintos
    niveles de ayuda. Solo accesible con sesión de admin activa."""
    if not session.get('admin_logged_in'):
        return redirect(url_for('serve_admin'))
    return send_from_directory(app.template_folder, 'prueba_niveles_ayuda.html')

@app.route('/api/config/pines', methods=['GET'])
def get_pines_publico():
    """Ruta pública (sin autenticación): las 4 versiones de la clave la
    consultan para saber dónde dibujar los pines sobre la foto de
    referencia. Si el admin nunca los ha ajustado, devuelve {} y el
    frontend usa sus coordenadas por defecto."""
    return jsonify(get_posiciones_pines())

@app.route('/api/config/zoom_caracteres', methods=['GET'])
def get_zoom_caracteres_publico():
    """Ruta pública: las 4 versiones de la clave la consultan para saber
    hacia dónde encuadrar la cámara (2D: zoom/pan sobre la foto; 3D:
    camera-orbit del modelo) cuando el estudiante pide ver un carácter
    específico de una pregunta. Vacío si el admin no lo ha calibrado."""
    return jsonify(get_zoom_caracteres())

@app.route('/admin')
def serve_admin():
    if session.get('admin_logged_in'):
        return send_from_directory(app.template_folder, 'admin.html')
    return send_from_directory(app.template_folder, 'admin_login.html')

# ===== API USUARIOS (PÚBLICAS) =====
ROLES_PARTICIPANTE = ['estudiante', 'docente', 'validador', 'pruebas']
_PALABRA_NOMBRE = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'’\-]+$")
_CORREO_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')

# Institución -> dominios de correo de sus ESTUDIANTES (para añadir otra institución, agregar una entrada aquí)
INSTITUCIONES = {'UPN': ['pedagogica.edu.co', 'upn.edu.co']}
# Estos tipos de participante pueden registrarse con cualquier correo
ROLES_CORREO_LIBRE = ['docente', 'validador', 'pruebas']

def dominios_estudiantes():
    return [d for ds in INSTITUCIONES.values() for d in ds]

def normalizar_nombre(texto):
    return re.sub(r'\s+', ' ', (texto or '')).strip()

def nombre_completo_valido(nombre):
    palabras = nombre.split(' ')
    return len(nombre) <= 50 and len(palabras) >= 2 and all(_PALABRA_NOMBRE.match(p) for p in palabras)

def dominio_de(correo):
    return correo.split('@')[-1].lower() if '@' in correo else ''

def _coincide(dominio, lista):
    return any(dominio == d or dominio.endswith('.' + d) for d in lista)

def correo_estudiante_valido(correo):
    dom = dominio_de(correo)
    return bool(dom) and _coincide(dom, dominios_estudiantes())

def institucion_de(correo):
    """Nombre de la institución según el dominio del correo (o el propio dominio si no está en INSTITUCIONES)."""
    dom = dominio_de(correo)
    for nombre, dominios in INSTITUCIONES.items():
        if _coincide(dom, dominios):
            return nombre
    return dom

@app.route('/api/config/registro', methods=['GET'])
def config_registro():
    """Público: opciones del formulario de registro (tipos de participante y dominios aceptados)."""
    return jsonify({'roles': ROLES_PARTICIPANTE, 'correo_libre': ROLES_CORREO_LIBRE, 'dominios_estudiante': dominios_estudiantes()})

# ===== SESIÓN DE APLICACIÓN Y CÓDIGO DE PARTICIPACIÓN =====
# Cada vez que el administrador ENTRA al panel se abre una sesión de aplicación nueva con un código de 6 caracteres. Solo quien escribe ese código
# (al registrarse o después, en su panel) entra al estudio y recibe grupo; quien no, se registra pero queda fuera. El código anterior deja de valer
# (con unos minutos de gracia por si el administrador vuelve a entrar en plena sesión), vence solo a las 12 h y puede cerrarse a mano.
import datetime as _dt
import secrets as _secrets

VIGENCIA_CODIGO_HORAS = 12
GRACIA_CODIGO_ANTERIOR_MIN = 10
ALFABETO_CODIGO = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'        # sin 0/O ni 1/I/L, para que no se confundan al dictarlo
LARGO_CODIGO = 6
MAX_FALLOS_CODIGO, VENTANA_FALLOS_MIN = 20, 10               # freno a los intentos de adivinar el código
MENSAJE_CODIGO_INVALIDO = 'El código no es válido o ya venció. Pídele a tu docente el código vigente de la sesión.'
MENSAJE_DEMASIADOS_INTENTOS = 'Demasiados intentos con códigos incorrectos. Espera unos minutos e inténtalo de nuevo.'
_fallos_codigo = {}

def _ahora_utc():
    return _dt.datetime.now(_dt.timezone.utc)

def _a_fecha(txt):
    return _dt.datetime.fromisoformat(txt)

def normalizar_codigo(txt):
    return re.sub(r'[^A-Za-z0-9]', '', str(txt or '')).upper()

def _leer_sesiones():
    c = Configuracion.query.filter_by(clave='sesiones_aplicacion').first()
    try:
        return json.loads(c.valor) if c and c.valor else []
    except Exception:
        return []

def _guardar_sesiones(lista):
    txt = json.dumps(lista[-300:], ensure_ascii=False)
    c = Configuracion.query.filter_by(clave='sesiones_aplicacion').first()
    if c: c.valor = txt
    else: db.session.add(Configuracion(clave='sesiones_aplicacion', valor=txt))
    db.session.commit()

def nueva_sesion_aplicacion(origen='ingreso'):
    lista, ahora = _leer_sesiones(), _ahora_utc()
    if lista and not lista[-1].get('reemplazada'):
        lista[-1]['reemplazada'] = ahora.isoformat()
    n = max([s['n'] for s in lista] + [0]) + 1
    codigo = ''.join(_secrets.choice(ALFABETO_CODIGO) for _ in range(LARGO_CODIGO))
    lista.append({'n': n, 'codigo': codigo, 'inicio': ahora.isoformat(), 'vence': (ahora + _dt.timedelta(hours=VIGENCIA_CODIGO_HORAS)).isoformat(),
                  'cerrada': None, 'reemplazada': None, 'origen': origen})
    _guardar_sesiones(lista)
    return lista[-1]

def estado_sesion(s, ahora=None):
    ahora = ahora or _ahora_utc()
    if s.get('cerrada'): return 'cerrada'
    if _a_fecha(s['vence']) <= ahora: return 'vencida'
    if s.get('reemplazada'): return 'reemplazada'
    return 'abierta'

def sesion_para_codigo(codigo):
    """Sesión a la que pertenece el código si TODAVÍA vale; si no, None. El de la sesión actual vale mientras no se cierre ni venza; el de la
    inmediatamente anterior, solo durante la gracia posterior a ser reemplazado."""
    c = normalizar_codigo(codigo)
    if len(c) != LARGO_CODIGO:
        return None
    ahora = _ahora_utc()
    for s in reversed(_leer_sesiones()):
        if s['codigo'] != c:
            continue
        est = estado_sesion(s, ahora)
        if est == 'abierta':
            return s
        if est == 'reemplazada' and ahora <= _a_fecha(s['reemplazada']) + _dt.timedelta(minutes=GRACIA_CODIGO_ANTERIOR_MIN):
            return s
        return None
    return None

def _clave_cliente():
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '?').split(',')[-1].strip()      # la última: la agrega el proxy de confianza

def codigo_bloqueado(clave):
    ahora = time.time()
    _fallos_codigo[clave] = [t for t in _fallos_codigo.get(clave, []) if ahora - t < VENTANA_FALLOS_MIN * 60]
    return len(_fallos_codigo[clave]) >= MAX_FALLOS_CODIGO

def registrar_fallo_codigo(clave):
    _fallos_codigo.setdefault(clave, []).append(time.time())

def _resumen_sesion(s):
    return {'n': s['n'], 'codigo': s['codigo'], 'estado': estado_sesion(s), 'inicio': s['inicio'], 'vence': s['vence'], 'origen': s.get('origen'),
            'participantes': Usuario.query.filter_by(sesion_aplicacion=s['n'], rol='estudiante').count()}

@app.route('/api/admin/sesion_aplicacion', methods=['GET'])
@admin_required
def get_sesion_aplicacion():
    lista = _leer_sesiones()
    return jsonify({'actual': _resumen_sesion(lista[-1]) if lista else None, 'historial': [_resumen_sesion(s) for s in reversed(lista[-8:-1])],
                    'vigencia_horas': VIGENCIA_CODIGO_HORAS, 'gracia_min': GRACIA_CODIGO_ANTERIOR_MIN})

@app.route('/api/admin/sesion_aplicacion/nueva', methods=['POST'])
@admin_required
def post_nueva_sesion_aplicacion():
    return jsonify({'actual': _resumen_sesion(nueva_sesion_aplicacion('manual'))})

@app.route('/api/admin/sesion_aplicacion/cerrar', methods=['POST'])
@admin_required
def post_cerrar_sesion_aplicacion():
    lista, ahora = _leer_sesiones(), _ahora_utc().isoformat()
    for s in lista[-2:]:                                  # cierra la actual Y la anterior (por si aún estaba en gracia)
        if not s.get('cerrada'): s['cerrada'] = ahora
    if lista: _guardar_sesiones(lista)
    return jsonify({'actual': _resumen_sesion(lista[-1]) if lista else None})

@app.route('/api/sesion/verificar_codigo', methods=['POST'])
def verificar_codigo_sesion():
    """Se usa ANTES de la prueba de rotación mental del registro: si el código está mal, se avisa enseguida y nadie pierde el test."""
    clave = _clave_cliente()
    if codigo_bloqueado(clave):
        return jsonify({'valido': False, 'error': MENSAJE_DEMASIADOS_INTENTOS}), 429
    if not sesion_para_codigo((request.json or {}).get('codigo')):
        registrar_fallo_codigo(clave)
        return jsonify({'valido': False, 'error': MENSAJE_CODIGO_INVALIDO}), 400
    return jsonify({'valido': True})

@app.route('/api/usuario/me/codigo_sesion', methods=['POST'])
def usuario_codigo_sesion():
    """Un estudiante que se registró sin código (o antes de la sesión) lo escribe en su panel y entra al estudio."""
    if 'usuario_id' not in session:
        return jsonify({'error': 'No autenticado'}), 401
    try:
        usuario = db.session.get(Usuario, session['usuario_id'])
        if not usuario:
            return jsonify({'error': 'No autenticado'}), 401
        if usuario.rol != 'estudiante':
            return jsonify({'error': 'Solo los estudiantes usan el código de la sesión'}), 400
        if usuario.grupo_asignado:
            return jsonify({'error': 'Ya tienes grupo asignado'}), 409
        claves = (_clave_cliente(), f'u{usuario.id}')
        if any(codigo_bloqueado(c) for c in claves):
            return jsonify({'error': MENSAJE_DEMASIADOS_INTENTOS}), 429
        s = sesion_para_codigo((request.json or {}).get('codigo'))
        if not s:
            for c in claves: registrar_fallo_codigo(c)
            return jsonify({'error': MENSAJE_CODIGO_INVALIDO}), 400
        usuario.sesion_aplicacion = s['n']
        if asignacion_automatica_activa():
            bloquear_asignacion()
            usuario.grupo_asignado = elegir_grupo_balanceado(usuario.semestre, usuario.genero)
        db.session.commit()
        return jsonify({'mensaje': 'Código correcto. Ya estás dentro de la sesión.', 'grupo_asignado': usuario.grupo_asignado, 'dentro_del_estudio': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# ===== ASIGNACIÓN AUTOMÁTICA DE GRUPO AL REGISTRARSE (solo estudiantes) =====
# Reparte a cada estudiante nuevo entre los 4 grupos de forma equilibrada DENTRO DE SU SEMESTRE (estratificación por semestre), para que un curso
# entero no caiga en un solo grupo. Regla: va al grupo con MENOS estudiantes de su semestre; si hay empate, al que tenga menos de su mismo género
# en ese semestre; luego al que tenga menos estudiantes en total; y si aún hay empate, al azar. Se puede apagar desde el panel (interruptor).
def asignacion_automatica_activa():
    c = Configuracion.query.filter_by(clave='asignacion_automatica').first()
    return not c or c.valor != '0'                                   # por defecto ACTIVADA

def bloquear_asignacion():
    """En PostgreSQL, serializa las asignaciones simultáneas (muchos estudiantes se registran a la vez en clase): si dos leyeran los mismos conteos,
    recibirían el mismo «mejor grupo». El candado se libera solo al terminar la transacción (commit o rollback)."""
    if db.engine.dialect.name == 'postgresql':
        from sqlalchemy import text as _sql
        db.session.execute(_sql('SELECT pg_advisory_xact_lock(727001)'))

def _semestre_entero(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None

# ===== CUPOS POR GRUPO: se llenan primero los grupos que van más atrás =====
# Cada grupo tiene un OBJETIVO de estudiantes (por defecto 25). Un estudiante nuevo solo se considera para los grupos que aún no lo alcanzaron y que
# estén entre los más atrasados (a menos de MARGEN_CUPO personas del más bajo); entre esos se aplica el equilibrio por semestre de siempre.
# Así los 4 grupos crecen parejos y, cuando todos llegan al objetivo, el reparto vuelve a ser parejo entre los cuatro.
OBJETIVO_POR_GRUPO_DEFECTO = 25
MARGEN_CUPO = 2
HORAS_PENDIENTE = 24

def objetivo_por_grupo():
    """Objetivo de estudiantes por grupo (0 = sin objetivo: se reparte parejo sin mirar los totales)."""
    c = Configuracion.query.filter_by(clave='objetivo_por_grupo').first()
    try:
        return max(0, int(c.valor)) if c and c.valor not in (None, '') else OBJETIVO_POR_GRUPO_DEFECTO
    except (TypeError, ValueError):
        return OBJETIVO_POR_GRUPO_DEFECTO

def efectivos_por_grupo():
    """Cuántos estudiantes cuentan hacia el objetivo de cada grupo: los que ya iniciaron el ejercicio (aunque sea un intento) y los registrados en las
    últimas 24 h, que aún pueden estar por hacerlo. Quien se registró hace más tiempo y nunca empezó NO cuenta (dejó un hueco que se debe llenar)."""
    limite = _dt.datetime.utcnow() - _dt.timedelta(hours=HORAS_PENDIENTE)
    con_sesion = db.session.query(SesionExperimental.usuario_id).distinct().subquery()
    filas = (db.session.query(Usuario.grupo_asignado, func.count(Usuario.id))
             .outerjoin(con_sesion, con_sesion.c.usuario_id == Usuario.id)
             .filter(Usuario.rol == 'estudiante', Usuario.grupo_asignado.isnot(None))
             .filter(or_(con_sesion.c.usuario_id.isnot(None), Usuario.fecha_registro >= limite))
             .group_by(Usuario.grupo_asignado).all())
    return {g: n for g, n in filas}

def _efectivos_todos():
    efect = efectivos_por_grupo()
    return {g: efect.get(g, 0) for g in GRUPOS_EN_ORDEN}

def grupos_candidatos():
    """Grupos entre los que se puede elegir ahora: los abiertos (bajo el objetivo) y más atrasados; si todos están completos, los cuatro."""
    objetivo = objetivo_por_grupo()
    if objetivo <= 0:
        return list(GRUPOS_EN_ORDEN)
    efect = efectivos_por_grupo()
    abiertos = [g for g in GRUPOS_EN_ORDEN if efect.get(g, 0) < objetivo]
    if not abiertos:
        return list(GRUPOS_EN_ORDEN)
    minimo = min(efect.get(g, 0) for g in abiertos)
    return [g for g in abiertos if efect.get(g, 0) <= minimo + MARGEN_CUPO]

def elegir_grupo_balanceado(semestre, genero):
    sem = _semestre_entero(semestre)
    est = db.session.query(Usuario.grupo_asignado, func.count(Usuario.id)).filter(Usuario.rol == 'estudiante', Usuario.grupo_asignado.isnot(None))
    filtro_sem = (Usuario.semestre == sem) if sem is not None else Usuario.semestre.is_(None)
    por_semestre = dict(est.filter(filtro_sem).group_by(Usuario.grupo_asignado).all())
    por_semestre_y_genero = dict(est.filter(filtro_sem, Usuario.genero == genero).group_by(Usuario.grupo_asignado).all())
    en_total = dict(est.group_by(Usuario.grupo_asignado).all())
    clave = lambda g: (por_semestre.get(g, 0), por_semestre_y_genero.get(g, 0), en_total.get(g, 0))
    candidatos = grupos_candidatos()                      # primero los cupos (grupos más atrasados); entre ellos, el equilibrio por semestre
    mejor = min(clave(g) for g in candidatos)
    return random.SystemRandom().choice([g for g in candidatos if clave(g) == mejor])

@app.route('/api/registro', methods=['POST'])
def registro():
    try:
        datos = request.json
        if not datos:
            return jsonify({'error': 'Datos inválidos'}), 400

        nombre = normalizar_nombre(datos.get('usuario'))
        correo = (datos.get('correo') or '').strip().lower()
        contrasena = datos.get('contrasena') or ''
        rol = (datos.get('rol') or '').strip().lower()

        if not nombre or not correo or not contrasena:
            return jsonify({'error': 'Nombre completo, correo y contraseña son obligatorios'}), 400
        if datos.get('consentimiento') is not True:
            return jsonify({'error': 'Debes aceptar el consentimiento informado para registrarte'}), 400
        if rol not in ROLES_PARTICIPANTE:
            return jsonify({'error': 'Selecciona el tipo de participante'}), 400
        if not nombre_completo_valido(nombre):
            return jsonify({'error': 'Escribe tu nombre completo: nombre y apellido, solo letras, máximo 50 caracteres'}), 400
        if not _CORREO_RE.match(correo):
            return jsonify({'error': 'Correo inválido'}), 400
        if rol not in ROLES_CORREO_LIBRE and not correo_estudiante_valido(correo):
            return jsonify({'error': 'Los estudiantes deben usar su correo institucional (' + ', '.join('@' + d for d in dominios_estudiantes()) + ')'}), 400
        if len(contrasena) < 6:
            return jsonify({'error': 'La contraseña debe tener al menos 6 caracteres'}), 400

        if Usuario.query.filter(func.lower(Usuario.nombre_usuario) == nombre.lower()).first():
            return jsonify({'error': 'Ya existe un usuario con ese nombre'}), 400
        if Usuario.query.filter(func.lower(Usuario.correo) == correo).first():
            return jsonify({'error': 'Correo ya registrado'}), 400

        # Código de la sesión de aplicación (solo estudiantes). Vacío = se registra pero queda FUERA del estudio; incorrecto o vencido = se avisa.
        sesion_app = None
        codigo_sesion = (datos.get('codigo_sesion') or '').strip()
        if rol == 'estudiante' and codigo_sesion:
            clave_cli = _clave_cliente()
            if codigo_bloqueado(clave_cli):
                return jsonify({'error': MENSAJE_DEMASIADOS_INTENTOS}), 429
            sesion_app = sesion_para_codigo(codigo_sesion)
            if not sesion_app:
                registrar_fallo_codigo(clave_cli)
                return jsonify({'error': MENSAJE_CODIGO_INVALIDO}), 400

        nuevo_usuario = Usuario(
            nombre_usuario=nombre,
            correo=correo,
            contrasena_hash=generate_password_hash(contrasena),
            semestre=datos.get('semestre'),
            institucion=institucion_de(correo),
            rol=rol,
            genero=datos.get('genero', 'No especificado'),
            experiencia_taxonomica=datos.get('experiencia', 3),
            habilidad_espacial=datos.get('habilidad_espacial', 12),
            familiaridad_3d=datos.get('familiaridad_3d', 3),
            conocimiento_genero=datos.get('conocimiento_genero', 1),
            puntaje_rotacion_mental=datos.get('puntaje_rotacion_mental'),
            rotacion_items=limpiar_rotacion_items(datos.get('rotacion_items'))
        )
        # Estudiantes: el grupo queda asignado desde el registro (al iniciar sesión ya lo tienen). Todo en la MISMA transacción que el alta del usuario.
        if rol == 'estudiante' and sesion_app:
                nuevo_usuario.sesion_aplicacion = sesion_app['n']
                if asignacion_automatica_activa():
                    bloquear_asignacion()
                    nuevo_usuario.grupo_asignado = elegir_grupo_balanceado(nuevo_usuario.semestre, nuevo_usuario.genero)
        db.session.add(nuevo_usuario)
        db.session.commit()
        return jsonify({'mensaje': 'Registro exitoso', 'id': nuevo_usuario.id, 'en_estudio': bool(sesion_app)}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/login', methods=['POST'])
def login():
    try:
        datos = request.json
        if not datos:
            return jsonify({'error': 'Datos inválidos'}), 400

        # Acepta el nombre completo (sin importar mayúsculas ni espacios de más) o el correo institucional
        entrada = normalizar_nombre(datos.get('usuario'))
        usuario = (Usuario.query.filter(func.lower(Usuario.nombre_usuario) == entrada.lower()).first()
                   or Usuario.query.filter(func.lower(Usuario.correo) == entrada.lower()).first())
        if usuario and check_password_hash(usuario.contrasena_hash, datos.get('contrasena', '')):
            session['usuario_id'] = usuario.id
            session['usuario_nombre'] = usuario.nombre_usuario
            return jsonify({
                'mensaje': 'Login exitoso',
                'usuario': usuario.nombre_usuario,
                'grupo': usuario.grupo_asignado,
                'rol': usuario.rol,
                'id': usuario.id
            })
        return jsonify({'error': 'Credenciales inválidas'}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('usuario_id', None)
    session.pop('usuario_nombre', None)
    return jsonify({'mensaje': 'Sesión cerrada'})

@app.route('/api/verificar_sesion', methods=['GET'])
def verificar_sesion():
    if session.get('usuario_id'):
        return jsonify({
            'activa': True,
            'usuario': session.get('usuario_nombre'),
            'id': session.get('usuario_id')
        })
    return jsonify({'activa': False}), 401

# ===== CUENTAS DE PRUEBAS: recorren solas los 4 grupos (2D -> 2D+M -> 3D -> 3D+M -> 2D...) =====
# Solo afecta a rol 'pruebas'. Un intento está TERMINADO cuando se guardaron las dos encuestas (SUS y carga cognitiva); en ese momento la cuenta
# pasa al grupo siguiente, para que el próximo intento use otra versión. Si el administrador cambió el grupo a mano durante el intento, no se toca.
GRUPOS_EN_ORDEN = ['2D', '2D_META', '3D', '3D_META']

def siguiente_grupo_pruebas(grupo):
    return GRUPOS_EN_ORDEN[(GRUPOS_EN_ORDEN.index(grupo) + 1) % len(GRUPOS_EN_ORDEN)] if grupo in GRUPOS_EN_ORDEN else GRUPOS_EN_ORDEN[0]

def sesion_completa(sesion):
    tipos = {e.tipo for e in ResultadoEncuesta.query.filter_by(sesion_id=sesion.id).all()}
    return 'SUS' in tipos and 'COGNITIVE_LOAD' in tipos


# ===== INTENTOS ANULADOS =====
# Un intento anulado se CONSERVA con todos sus datos (para poder analizarlo si hace falta), pero deja de contar: no aparece en el panel del
# participante, ni en las estadísticas, ni en la exportación principal. Así se puede habilitar un intento nuevo sin borrar nada.
SESION_VIGENTE = or_(SesionExperimental.anulada.is_(False), SesionExperimental.anulada.is_(None))

def sesiones_validas(usuario):
    return [s for s in usuario.sesiones if not s.anulada]

def anular_sesion(sesion, motivo, por):
    sesion.anulada = True
    sesion.motivo_anulacion = (motivo or 'No especificado')[:500]
    sesion.fecha_anulacion = _dt.datetime.utcnow()
    sesion.anulada_por = por

def sesion_visible_pruebas(usuario):
    """Intento más reciente que ya tiene al menos un resultado: es el que muestra el panel (los anteriores quedan como historial)."""
    for s in sorted(sesiones_validas(usuario), key=lambda x: x.id, reverse=True):
        if s.resultados:
            return s
    return None

def sesiones_visibles(usuario):
    if usuario.rol == 'pruebas':
        s = sesion_visible_pruebas(usuario)
        return [s] if s else []
    return sesiones_validas(usuario)

def progreso_pruebas(usuario):
    if usuario.rol != 'pruebas':
        return None
    hechos = []
    for s in sorted(sesiones_validas(usuario), key=lambda x: x.id):
        if s.grupo and s.grupo not in hechos and sesion_completa(s):
            hechos.append(s.grupo)
    return {'completados': hechos, 'siguiente': usuario.grupo_asignado, 'orden': GRUPOS_EN_ORDEN}

def avanzar_grupo_pruebas(usuario_id, sesion_id):
    usuario, sesion = db.session.get(Usuario, usuario_id), db.session.get(SesionExperimental, sesion_id)
    if not usuario or not sesion or usuario.rol != 'pruebas' or sesion.usuario_id != usuario.id:
        return None
    if not sesion_completa(sesion) or usuario.grupo_asignado != sesion.grupo:
        return None
    usuario.grupo_asignado = siguiente_grupo_pruebas(sesion.grupo)
    db.session.commit()
    return usuario.grupo_asignado

@app.route('/api/usuario/me', methods=['GET'])
def get_usuario_me():
    """Devuelve los datos del usuario con sesión activa."""
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    usuario = Usuario.query.get(session['usuario_id'])
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    return jsonify({
        'id': usuario.id,
        'nombre_usuario': usuario.nombre_usuario,
        'correo': usuario.correo,
        'grupo_asignado': usuario.grupo_asignado,
        'semestre': usuario.semestre,
        'curso': usuario.curso,
        'experiencia_taxonomica': usuario.experiencia_taxonomica,
        'habilidad_espacial': usuario.habilidad_espacial,
        'familiaridad_3d': usuario.familiaridad_3d,
        'conocimiento_genero': usuario.conocimiento_genero,
        'puntaje_rotacion_mental': usuario.puntaje_rotacion_mental, 'rotacion_items': rotacion_items_dict(usuario),
        'rol': usuario.rol,
        'institucion': usuario.institucion,
        'pruebas': progreso_pruebas(usuario),
    })

@app.route('/api/usuario/me/resultados', methods=['GET'])
def get_resultados_me():
    """Devuelve los resultados del usuario con sesión activa."""
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    usuario = Usuario.query.get(session['usuario_id'])
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    resultados = []
    for sesion_exp in sesiones_visibles(usuario):
        for r in sesion_exp.resultados:
            resultados.append({
                'sesion_id': sesion_exp.id,
                'grupo': sesion_exp.grupo,
                'especimen': r.especie_id,
                'correcta': r.especie_correcta,
                'seleccionada': r.especie_seleccionada,
                'acerto': r.es_correcta,
                'tiempo': r.tiempo_segundos,
                'tiempo_reflexion': r.tiempo_reflexion_segundos,
                'primera_pregunta_desvio': r.primera_pregunta_desvio,
                'ayuda_encuadre_usos': r.ayuda_encuadre_usos or 0,
                'ayuda_encuadre_detalle': r.ayuda_encuadre_detalle,
                'orden': r.orden
            })
    return jsonify(resultados)

@app.route('/api/usuario/me/encuestas/completadas', methods=['GET'])
def encuestas_completadas_me():
    """Verifica encuestas completadas del usuario con sesión activa."""
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    try:
        sesion_exp = SesionExperimental.query.filter_by(
            usuario_id=session['usuario_id']
        ).filter(SESION_VIGENTE).order_by(SesionExperimental.id.desc()).first()
        _u = db.session.get(Usuario, session['usuario_id'])
        if _u and _u.rol == 'pruebas':
            sesion_exp = sesion_visible_pruebas(_u) or sesion_exp

        if not sesion_exp:
            return jsonify({
                'sus_completada': False,
                'carga_completada': False,
                'ambas_completadas': False,
                'sesion_id': None
            })

        encuestas = ResultadoEncuesta.query.filter_by(sesion_id=sesion_exp.id).all()
        sus_completada = any(e.tipo == 'SUS' for e in encuestas)
        carga_completada = any(e.tipo == 'COGNITIVE_LOAD' for e in encuestas)

        return jsonify({
            'sus_completada': sus_completada,
            'carga_completada': carga_completada,
            'ambas_completadas': sus_completada and carga_completada,
            'sesion_id': sesion_exp.id
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/usuario/me/pasos', methods=['GET'])
def get_pasos_me():
    """Devuelve el recorrido detallado (paso a paso) del usuario con
    sesión activa: en qué pregunta estaba, qué eligió, si acertó ese
    paso, y cuánto tiempo tardó. Usado en el dashboard del estudiante
    para mostrar en qué falló y con qué tiempo respondió cada paso."""
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    usuario = Usuario.query.get(session['usuario_id'])
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    pasos = []
    for sesion_exp in sesiones_visibles(usuario):
        for p in sesion_exp.pasos:
            pasos.append({
                'sesion_id': sesion_exp.id,
                'especimen_id': p.especimen_id,
                'especimen_orden': p.especimen_orden,
                'pregunta': p.pregunta,
                'opcion_elegida': p.opcion_elegida,
                'es_paso_correcto': p.es_paso_correcto,
                'es_paso_final': p.es_paso_final,
                'tiempo_segundos': p.tiempo_segundos,
                'orden_paso': p.orden_paso,
                'timestamp': p.timestamp.isoformat() if p.timestamp else None
            })
    return jsonify(pasos)

# Rutas legacy por compatibilidad con HTML existente (redirigen a /me)
@app.route('/api/usuario/<nombre_usuario>', methods=['GET'])
def get_usuario_by_nombre(nombre_usuario):
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    usuario = Usuario.query.filter_by(nombre_usuario=nombre_usuario).first()
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    if usuario.id != session['usuario_id']:
        return jsonify({'error': 'Acceso no autorizado'}), 403
    return jsonify({
        'id': usuario.id,
        'nombre_usuario': usuario.nombre_usuario,
        'correo': usuario.correo,
        'grupo_asignado': usuario.grupo_asignado,
        'semestre': usuario.semestre,
        'curso': usuario.curso,
        'experiencia_taxonomica': usuario.experiencia_taxonomica,
        'habilidad_espacial': usuario.habilidad_espacial,
        'familiaridad_3d': usuario.familiaridad_3d,
        'conocimiento_genero': usuario.conocimiento_genero,
        'puntaje_rotacion_mental': usuario.puntaje_rotacion_mental, 'rotacion_items': rotacion_items_dict(usuario),
        'rol': usuario.rol,
        'institucion': usuario.institucion,
    })

@app.route('/api/usuario/<int:usuario_id>/resultados', methods=['GET'])
def get_resultados_usuario(usuario_id):
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    if usuario_id != session['usuario_id']:
        return jsonify({'error': 'Acceso no autorizado'}), 403
    usuario = Usuario.query.get(usuario_id)
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    resultados = []
    for sesion_exp in sesiones_validas(usuario):
        for r in sesion_exp.resultados:
            resultados.append({
                'sesion_id': sesion_exp.id,
                'especimen': r.especie_id,
                'correcta': r.especie_correcta,
                'seleccionada': r.especie_seleccionada,
                'acerto': r.es_correcta,
                'tiempo': r.tiempo_segundos,
                'tiempo_reflexion': r.tiempo_reflexion_segundos,
                'primera_pregunta_desvio': r.primera_pregunta_desvio,
                'ayuda_encuadre_usos': r.ayuda_encuadre_usos or 0,
                'ayuda_encuadre_detalle': r.ayuda_encuadre_detalle,
                'orden': r.orden
            })
    return jsonify(resultados)

@app.route('/api/usuario/<int:usuario_id>/encuestas/completadas', methods=['GET'])
def encuestas_completadas(usuario_id):
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    if usuario_id != session['usuario_id']:
        return jsonify({'error': 'Acceso no autorizado'}), 403
    try:
        sesion_exp = SesionExperimental.query.filter_by(
            usuario_id=usuario_id
        ).filter(SESION_VIGENTE).order_by(SesionExperimental.id.desc()).first()

        if not sesion_exp:
            return jsonify({
                'sus_completada': False,
                'carga_completada': False,
                'ambas_completadas': False,
                'sesion_id': None
            })

        encuestas = ResultadoEncuesta.query.filter_by(sesion_id=sesion_exp.id).all()
        sus_completada = any(e.tipo == 'SUS' for e in encuestas)
        carga_completada = any(e.tipo == 'COGNITIVE_LOAD' for e in encuestas)

        return jsonify({
            'sus_completada': sus_completada,
            'carga_completada': carga_completada,
            'ambas_completadas': sus_completada and carga_completada,
            'sesion_id': sesion_exp.id
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===== API ADMIN LOGIN =====
@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.json
    if not data:
        return jsonify({'error': 'Datos inválidos'}), 400

    username = data.get('username', '').strip()
    password = data.get('password', '')

    if username == ADMIN_USER and password == ADMIN_PASSWORD:
        session['admin_logged_in'] = True
        session['admin_user'] = username
        try:
            nueva_sesion_aplicacion('ingreso')       # cada ingreso al panel = nueva sesión de aplicación con código nuevo
        except Exception as e:
            db.session.rollback(); print('[sesion] No se pudo abrir la sesión de aplicación:', e, flush=True)
        return jsonify({'success': True, 'message': 'Login exitoso'})

    return jsonify({'success': False, 'error': 'Credenciales incorrectas'}), 401

@app.route('/api/admin/verificar', methods=['GET'])
def verificar_admin():
    if session.get('admin_logged_in'):
        return jsonify({'authenticated': True, 'user': session.get('admin_user')})
    return jsonify({'authenticated': False}), 401

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin_logged_in', None)
    session.pop('admin_user', None)
    return jsonify({'success': True, 'message': 'Sesión cerrada'})

# ===== API ADMIN (PROTEGIDAS) =====
@app.route('/api/admin/usuarios', methods=['GET'])
@admin_required
def get_usuarios():
    usuarios = Usuario.query.all()
    sesiones_por_usuario = dict(db.session.query(SesionExperimental.usuario_id, func.count(SesionExperimental.id)).filter(SESION_VIGENTE).group_by(SesionExperimental.usuario_id).all())
    anuladas_por_usuario = dict(db.session.query(SesionExperimental.usuario_id, func.count(SesionExperimental.id)).filter(SesionExperimental.anulada.is_(True)).group_by(SesionExperimental.usuario_id).all())
    return jsonify([{
        'id': u.id,
        'n_sesiones': sesiones_por_usuario.get(u.id, 0),
        'n_anuladas': anuladas_por_usuario.get(u.id, 0),
        'nombre_usuario': u.nombre_usuario,
        'correo': u.correo,
        'semestre': u.semestre,
        'curso': u.curso,
        'genero': u.genero,
        'experiencia_taxonomica': u.experiencia_taxonomica,
        'habilidad_espacial': u.habilidad_espacial,
        'familiaridad_3d': u.familiaridad_3d,
        'conocimiento_genero': u.conocimiento_genero,
        'puntaje_rotacion_mental': u.puntaje_rotacion_mental, 'rotacion_items': rotacion_items_dict(u),
        'rol': u.rol,
        'institucion': u.institucion,
        'grupo_asignado': u.grupo_asignado, 'sesion_aplicacion': u.sesion_aplicacion,
        'fecha_registro': u.fecha_registro.isoformat() if u.fecha_registro else None
    } for u in usuarios])

# ===== PANTALLA DE GRACIAS: imágenes propias opcionales =====
# Se suben al repositorio (en Render los archivos subidos en ejecución se pierden al redesplegar, por eso van en GitHub).
# Se revisan DOS carpetas: frontend/static/gracias/ y frontend/static/imagenes/gracias/ (si un nombre está en las dos, vale la primera).
# Por el NOMBRE del archivo se sabe dónde se usa: principal* (reemplaza al 🎉 grande), fila* (reemplaza a 🙏🐜💙) y confeti* (reemplaza al
# confeti de emojis). Si no hay imágenes, se usan los emojis.
EXT_IMAGENES_GRACIAS = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg')
CARPETAS_GRACIAS = ('gracias', 'imagenes/gracias')

@app.route('/api/config/gracias_imagenes', methods=['GET'])
def gracias_imagenes():
    salida = {'principal': None, 'fila': [], 'confeti': []}
    archivos, vistos = [], set()
    for carpeta in CARPETAS_GRACIAS:
        ruta = os.path.join(app.static_folder, *carpeta.split('/'))
        try:
            nombres = os.listdir(ruta)
        except OSError:
            continue
        for nombre in nombres:
            if nombre.lower() in vistos or not os.path.isfile(os.path.join(ruta, nombre)):
                continue
            vistos.add(nombre.lower()); archivos.append((nombre, carpeta))
    for nombre, carpeta in sorted(archivos, key=lambda x: x[0].lower()):        # orden predecible, sin distinguir mayúsculas
        base, ext = os.path.splitext(nombre)
        if ext.lower() not in EXT_IMAGENES_GRACIAS or not re.match(r'^[\w\- ]+$', base):
            continue
        url = url_for('static', filename=carpeta + '/' + nombre)
        bajo = base.lower()
        if bajo.startswith('principal') and salida['principal'] is None: salida['principal'] = url
        elif bajo.startswith('fila') and len(salida['fila']) < 6: salida['fila'].append(url)
        elif bajo.startswith('confeti') and len(salida['confeti']) < 12: salida['confeti'].append(url)
    return jsonify(salida)

@app.route('/api/admin/asignar_grupo', methods=['POST'])
@admin_required
def asignar_grupo():
    try:
        data = request.json
        grupos_validos = ['2D', '2D_META', '3D', '3D_META']
        if data.get('grupo') not in grupos_validos:
            return jsonify({'error': 'Grupo inválido'}), 400
        usuario = Usuario.query.get(data['usuario_id'])
        if not usuario:
            return jsonify({'error': 'Usuario no encontrado'}), 404
        nuevo, actual = data['grupo'], usuario.grupo_asignado
        sesiones = SesionExperimental.query.filter_by(usuario_id=usuario.id).filter(SESION_VIGENTE).count()
        # Cuentas de PRUEBAS: se cambia libremente (cada intento nuevo toma el grupo que tengan en ese momento).
        # Participantes REALES que ya hicieron el ejercicio: cambiar el grupo mezclaría los datos del estudio; solo con confirmación explícita.
        if actual and actual != nuevo and usuario.rol != 'pruebas' and sesiones > 0:
            if not data.get('forzar'):
                return jsonify({'error': f'{usuario.nombre_usuario} ya tiene {sesiones} sesión(es) en el grupo {actual}. Cambiar su grupo ahora mezclaría los datos del estudio.',
                                'codigo': 'ya_participo', 'grupo_actual': actual, 'sesiones': sesiones}), 409
            print(f'[admin] Cambio de grupo FORZADO: {usuario.nombre_usuario} (id {usuario.id}) {actual} -> {nuevo} con {sesiones} sesión(es) previas', flush=True)
        usuario.grupo_asignado = nuevo
        db.session.commit()
        return jsonify({'mensaje': f'Grupo {nuevo} asignado a {usuario.nombre_usuario}', 'rol': usuario.rol, 'grupo_anterior': actual})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/objetivo_grupo', methods=['GET'])
@admin_required
def get_objetivo_grupo():
    efect = efectivos_por_grupo()
    return jsonify({'objetivo': objetivo_por_grupo(), 'efectivos': {g: efect.get(g, 0) for g in GRUPOS_EN_ORDEN}, 'margen': MARGEN_CUPO, 'candidatos': grupos_candidatos(), 'horas_pendiente': HORAS_PENDIENTE})

@app.route('/api/admin/objetivo_grupo', methods=['POST'])
@admin_required
def post_objetivo_grupo():
    try:
        valor = int((request.json or {}).get('objetivo'))
    except (TypeError, ValueError):
        return jsonify({'error': 'El objetivo debe ser un número entero (0 = sin objetivo)'}), 400
    if not 0 <= valor <= 500:
        return jsonify({'error': 'El objetivo debe estar entre 0 y 500'}), 400
    c = Configuracion.query.filter_by(clave='objetivo_por_grupo').first()
    if c: c.valor = str(valor)
    else: db.session.add(Configuracion(clave='objetivo_por_grupo', valor=str(valor)))
    db.session.commit()
    return jsonify({'objetivo': valor, 'candidatos': grupos_candidatos()})

@app.route('/api/admin/balance_grupos', methods=['GET'])
@admin_required
def balance_grupos():
    """Estudiantes por semestre y grupo: cuántos hay registrados y cuántos ya completaron la identificación (2 resultados)."""
    completos = {uid for uid, n in db.session.query(SesionExperimental.usuario_id, func.count(ResultadoIdentificacion.id))
                 .join(ResultadoIdentificacion, ResultadoIdentificacion.sesion_id == SesionExperimental.id).filter(SESION_VIGENTE).group_by(SesionExperimental.usuario_id).all() if n >= 2}
    celdas, sin_grupo = {}, 0
    for u in Usuario.query.filter_by(rol='estudiante').all():
        if not u.grupo_asignado:
            sin_grupo += 1
            continue
        c = celdas.setdefault(u.semestre, {g: {'registrados': 0, 'completaron': 0} for g in GRUPOS_EN_ORDEN})[u.grupo_asignado]
        c['registrados'] += 1
        c['completaron'] += 1 if u.id in completos else 0
    filas = [{'semestre': s, 'grupos': celdas[s]} for s in sorted(celdas, key=lambda x: (x is None, x if x is not None else 0))]
    totales = {g: {'registrados': sum(f['grupos'][g]['registrados'] for f in filas), 'completaron': sum(f['grupos'][g]['completaron'] for f in filas)} for g in GRUPOS_EN_ORDEN}
    return jsonify({'activa': asignacion_automatica_activa(), 'grupos': GRUPOS_EN_ORDEN, 'filas': filas, 'totales': totales, 'sin_grupo': sin_grupo,
                    'objetivo': objetivo_por_grupo(), 'efectivos': _efectivos_todos(), 'margen': MARGEN_CUPO})

@app.route('/api/admin/asignacion_automatica', methods=['POST'])
@admin_required
def set_asignacion_automatica():
    try:
        activa = bool((request.json or {}).get('activa'))
        c = Configuracion.query.filter_by(clave='asignacion_automatica').first()
        if c: c.valor = '1' if activa else '0'
        else: db.session.add(Configuracion(clave='asignacion_automatica', valor='1' if activa else '0'))
        db.session.commit()
        return jsonify({'activa': activa})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/asignar_grupo_lote', methods=['POST'])
@admin_required
def asignar_grupo_lote():
    """Asigna grupos automáticamente en rotación a usuarios sin grupo."""
    try:
        usuarios_sin_grupo = Usuario.query.filter_by(grupo_asignado=None).order_by(Usuario.id).all()
        asignados, estudiantes, sin_codigo, rotacion = 0, 0, 0, 0
        bloquear_asignacion()
        for u in usuarios_sin_grupo:
            if u.rol == 'estudiante':
                if u.sesion_aplicacion is None:                 # no escribió el código de ninguna sesión: no es parte del estudio; se asigna a mano si hace falta
                    sin_codigo += 1
                    continue
                u.grupo_asignado = elegir_grupo_balanceado(u.semestre, u.genero)      # equilibrado por semestre, contando a los ya asignados
                estudiantes += 1
            else:
                u.grupo_asignado = GRUPOS_EN_ORDEN[rotacion % len(GRUPOS_EN_ORDEN)]    # docentes, validadores y pruebas: rotación simple
                rotacion += 1
            asignados += 1
        db.session.commit()
        return jsonify({'mensaje': f'{asignados} usuarios asignados ({estudiantes} estudiantes con equilibrio por semestre). {sin_codigo} estudiantes sin código se dejaron sin grupo.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/config/especies', methods=['GET'])
@admin_required
def get_especies_config():
    return jsonify({'todas': POOL_ESPECIES, 'activas': get_especies_activas()})

@app.route('/api/admin/config/especies', methods=['POST'])
@admin_required
def set_especies_config():
    try:
        data = request.json
        ids_validos = [e['id'] for e in POOL_ESPECIES]
        activas = [e for e in data.get('activas', []) if e in ids_validos]
        if len(activas) < 2:
            return jsonify({'error': 'Activa al menos 2 especies: cada participante identifica 2 especies distintas'}), 400
        set_especies_activas(activas)
        return jsonify({'mensaje': 'Configuración actualizada'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/config/pines', methods=['POST'])
@admin_required
def set_pines_admin():
    """El admin ajusta la posición de los pines del glosario interactivo
    arrastrándolos en el panel; esto guarda las coordenadas finales
    (en % sobre la imagen) para que las 4 versiones de la clave las usen."""
    try:
        data = request.json
        posiciones = data.get('posiciones', {})
        for pid, coords in posiciones.items():
            if not isinstance(coords, dict) or 'px' not in coords or 'py' not in coords:
                return jsonify({'error': f'Formato inválido para el pin "{pid}"'}), 400
        set_posiciones_pines(posiciones)
        return jsonify({'mensaje': 'Posiciones de pines guardadas'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/config/zoom_caracteres', methods=['POST'])
@admin_required
def set_zoom_caracteres_admin():
    """El admin calibra, por pregunta, hacia dónde debe apuntar la cámara
    (2D: x%, y%, escala sobre la foto; 3D: camera-orbit/camera-target del
    modelo) para que el botón 'Ver este carácter' lleve la vista ahí."""
    try:
        data = request.json
        datos = data.get('datos', {})
        set_zoom_caracteres(datos)
        return jsonify({'mensaje': 'Encuadres de caracteres guardados'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===== PRUEBA DE ROTACIÓN MENTAL: respuestas ítem por ítem =====
# Se guardan para poder estimar la fiabilidad (KR-20), la dificultad y la discriminación de cada ítem con los datos reales.
def limpiar_rotacion_items(obj):
    """Valida y normaliza el detalle que envía el registro. Devuelve el texto JSON a guardar, o None si no es válido (el registro no se rechaza)."""
    try:
        if not isinstance(obj, dict) or not isinstance(obj.get('items'), list) or not (1 <= len(obj['items']) <= 12):
            return None
        items = []
        for it in obj['items']:
            sel, cor = str(it.get('sel', '')), str(it.get('cor', ''))
            if not set(sel) <= set('ABCD') or not set(cor) <= set('ABCD') or len(sel) > 2 or len(cor) != 2:
                return None
            ok = 1 if (len(sel) == 2 and set(sel) <= set(cor)) else 0      # se recalcula aquí: 1 punto solo si marca exactamente las dos correctas
            items.append({'i': int(it['i']), 's': int(it.get('s', 0)), 'sel': sel, 'cor': cor, 'ok': ok,
                          'visto': bool(it.get('visto', False)), 't': round(min(max(float(it.get('t', 0)), 0), 600), 1)})
        limpio = {'v': 1, 'items': items, 'total_s': round(min(max(float(obj.get('total_s', 0)), 0), 600), 1),
                  'por_tiempo': bool(obj.get('por_tiempo', False)), 'alcanzados': sum(1 for x in items if x['visto'])}
        return json.dumps(limpio, ensure_ascii=False)
    except Exception:
        return None

def rotacion_items_dict(usuario):
    try:
        return json.loads(usuario.rotacion_items) if usuario.rotacion_items else None
    except Exception:
        return None

ROT_COLUMNAS = [f'rot_i{n}' for n in range(1, 13)] + [f'rot_t{n}' for n in range(1, 13)] + ['rot_alcanzados', 'rot_total_s', 'rot_por_tiempo']

def columnas_rotacion(usuario):
    d = rotacion_items_dict(usuario)
    if not d:
        return [''] * len(ROT_COLUMNAS)
    por_i = {x['i']: x for x in d['items']}
    acierto = [(por_i[n]['ok'] if n in por_i and por_i[n]['visto'] else '') for n in range(1, 13)]
    tiempos = [(por_i[n]['t'] if n in por_i and por_i[n]['visto'] else '') for n in range(1, 13)]
    return acierto + tiempos + [d.get('alcanzados', ''), d.get('total_s', ''), 1 if d.get('por_tiempo') else 0]

# ===== COPIA DE SEGURIDAD DE LA CONFIGURACIÓN (especies activas, pines y encuadres de zoom) =====
# Las bases gratuitas de Render vencen a los 30 días y no tienen respaldos. Estos dos endpoints permiten descargar la
# configuración a un archivo y restaurarla en una base nueva (por ejemplo, al pasar a Neon) sin recalibrar nada a mano.
CLAVES_COPIA_CONFIG = {'especies_activas': list, 'posiciones_pines': dict, 'zoom_caracteres': dict}

@app.route('/api/admin/config/copia', methods=['GET'])
@admin_required
def descargar_copia_config():
    from datetime import datetime
    contenido = {}
    for clave in CLAVES_COPIA_CONFIG:
        c = Configuracion.query.filter_by(clave=clave).first()
        if c and c.valor:
            try:
                contenido[clave] = json.loads(c.valor)
            except Exception:
                pass
    return jsonify({'tipo': 'copia_configuracion_clave_linepithema', 'version': 1,
                    'fecha': datetime.utcnow().isoformat() + 'Z', 'configuracion': contenido})

@app.route('/api/admin/config/restaurar', methods=['POST'])
@admin_required
def restaurar_copia_config():
    d = request.get_json(silent=True) or {}
    if d.get('tipo') != 'copia_configuracion_clave_linepithema' or not isinstance(d.get('configuracion'), dict):
        return jsonify({'error': 'El archivo no es una copia de configuración de esta plataforma'}), 400
    cfg, nuevos = d['configuracion'], {}
    # Primero se valida TODO; solo si todo es correcto se guarda (no queda una restauración a medias)
    for clave, tipo in CLAVES_COPIA_CONFIG.items():
        if clave not in cfg:
            continue
        if not isinstance(cfg[clave], tipo):
            return jsonify({'error': f'El bloque "{clave}" del archivo tiene un formato inválido'}), 400
        if clave == 'especies_activas':
            validos = {e['id'] for e in POOL_ESPECIES}
            ids = [i for i in cfg[clave] if i in validos]
            if len(ids) < 2:
                return jsonify({'error': 'La copia trae menos de 2 especies activas válidas'}), 400
            nuevos[clave] = ids
        else:
            nuevos[clave] = cfg[clave]
    if not nuevos:
        return jsonify({'error': 'La copia no contiene configuración para restaurar'}), 400
    try:
        for clave, valor in nuevos.items():
            c = Configuracion.query.filter_by(clave=clave).first()
            if c:
                c.valor = json.dumps(valor)
            else:
                db.session.add(Configuracion(clave=clave, valor=json.dumps(valor)))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'restauradas': sorted(nuevos)})

# ===== COPIA COMPLETA DE LA BASE (para cambiar de proveedor sin perder participantes ni resultados) =====
# Exporta las 7 tablas con sus identificadores y las restaura en una base NUEVA y VACÍA (Neon, otra base de Render, etc.).
# Conserva las cuentas con su contraseña, las sesiones, los resultados, los pasos, las reflexiones, las encuestas y la configuración,
# así que ningún estudiante puede repetir el ejercicio ni pierde su certificado. El archivo contiene nombres, correos y contraseñas
# cifradas: debe guardarse como datos de investigación (acceso restringido).
ORDEN_COPIA_COMPLETA = [('usuarios', Usuario), ('sesiones', SesionExperimental), ('resultados', ResultadoIdentificacion),
                        ('encuestas', ResultadoEncuesta), ('reflexiones', ReflexionMetacognitiva), ('pasos_clave', PasoClave),
                        ('configuracion', Configuracion)]
TABLAS_CON_SESION = ('resultados', 'encuestas', 'reflexiones', 'pasos_clave')

def _a_json(valor):
    from datetime import datetime
    return valor.isoformat() if isinstance(valor, datetime) else valor

@app.route('/api/admin/migracion/exportar', methods=['GET'])
@admin_required
def exportar_copia_completa():
    from datetime import datetime
    tablas = {}
    for nombre, modelo in ORDEN_COPIA_COMPLETA:
        columnas = [c.name for c in modelo.__table__.columns]
        tablas[nombre] = [{c: _a_json(getattr(fila, c)) for c in columnas} for fila in modelo.query.order_by(modelo.id).all()]
    return jsonify({'tipo': 'copia_completa_clave_linepithema', 'version': 1, 'fecha': datetime.utcnow().isoformat() + 'Z',
                    'conteos': {n: len(f) for n, f in tablas.items()}, 'tablas': tablas})

@app.route('/api/admin/migracion/importar', methods=['POST'])
@admin_required
def importar_copia_completa():
    from datetime import datetime
    d = request.get_json(silent=True) or {}
    if d.get('tipo') != 'copia_completa_clave_linepithema' or not isinstance(d.get('tablas'), dict):
        return jsonify({'error': 'El archivo no es una copia completa de esta plataforma'}), 400
    tablas = d['tablas']
    # 1) La base de destino debe estar VACÍA de participantes y resultados (si no, se duplicarían o se mezclarían)
    ocupadas = [n for n, m in ORDEN_COPIA_COMPLETA if n != 'configuracion' and m.query.first() is not None]
    if ocupadas:
        return jsonify({'error': 'La base de destino ya tiene datos (' + ', '.join(ocupadas) + '). La restauración completa solo se hace en una base nueva y vacía, ANTES de crear cuentas.'}), 409
    # 2) Se valida y se convierte todo antes de escribir nada
    filas_por_tabla = {}
    for nombre, modelo in ORDEN_COPIA_COMPLETA:
        filas = tablas.get(nombre, [])
        if not isinstance(filas, list):
            return jsonify({'error': f'La tabla "{nombre}" del archivo tiene un formato inválido'}), 400
        columnas = {c.name: c for c in modelo.__table__.columns}
        convertidas, ids = [], set()
        for i, fila in enumerate(filas):
            if not isinstance(fila, dict) or not set(fila) <= set(columnas):
                return jsonify({'error': f'Fila {i + 1} de "{nombre}": columnas desconocidas o formato inválido'}), 400
            nueva = {}
            for col, valor in fila.items():
                tipo = str(columnas[col].type).upper()
                if valor is not None and tipo.startswith('DATETIME') or (valor is not None and 'TIMESTAMP' in tipo):
                    try:
                        valor = datetime.fromisoformat(str(valor).replace('Z', ''))
                    except ValueError:
                        return jsonify({'error': f'Fila {i + 1} de "{nombre}": fecha inválida en "{col}"'}), 400
                nueva[col] = valor
            if nombre != 'configuracion':
                if nueva.get('id') in ids or nueva.get('id') is None:
                    return jsonify({'error': f'Fila {i + 1} de "{nombre}": identificador repetido o ausente'}), 400
                ids.add(nueva['id'])
            convertidas.append(nueva)
        filas_por_tabla[nombre] = convertidas
    ids_usuarios = {f['id'] for f in filas_por_tabla['usuarios']}
    ids_sesiones = {f['id'] for f in filas_por_tabla['sesiones']}
    if any(f.get('usuario_id') not in ids_usuarios for f in filas_por_tabla['sesiones']):
        return jsonify({'error': 'Hay sesiones que apuntan a un usuario que no está en la copia'}), 400
    for t in TABLAS_CON_SESION:
        if any(f.get('sesion_id') not in ids_sesiones for f in filas_por_tabla[t]):
            return jsonify({'error': f'Hay filas de "{t}" que apuntan a una sesión que no está en la copia'}), 400
    # 3) Escritura en una sola transacción: o entra todo o no entra nada
    try:
        for nombre, modelo in ORDEN_COPIA_COMPLETA:
            if nombre == 'configuracion':
                for fila in filas_por_tabla[nombre]:
                    existente = Configuracion.query.filter_by(clave=fila.get('clave')).first()
                    if existente:
                        existente.valor = fila.get('valor')
                    else:
                        fila = {k: v for k, v in fila.items() if k != 'id'}
                        db.session.add(Configuracion(**fila))
            elif filas_por_tabla[nombre]:
                db.session.bulk_insert_mappings(modelo, filas_por_tabla[nombre])
        db.session.flush()
        # En PostgreSQL, los contadores automáticos de identificadores deben continuar DESPUÉS del último importado;
        # si no, la siguiente cuenta o resultado nuevo chocaría con uno restaurado.
        if db.engine.dialect.name == 'postgresql':
            for nombre, modelo in ORDEN_COPIA_COMPLETA:
                tabla = modelo.__table__.name
                db.session.execute(db.text(f"SELECT setval(pg_get_serial_sequence('{tabla}', 'id'), COALESCE((SELECT MAX(id) FROM {tabla}), 1), (SELECT MAX(id) FROM {tabla}) IS NOT NULL)"))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'No se pudo restaurar (no se guardó nada): ' + str(e).splitlines()[0][:200]}), 500
    return jsonify({'ok': True, 'restaurado': {n: len(f) for n, f in filas_por_tabla.items()}})

@app.route('/api/admin/datos', methods=['GET'])
@admin_required
def ver_datos():
    resultados = []
    for sesion_exp in SesionExperimental.query.filter(SESION_VIGENTE).all():
        usuario = Usuario.query.get(sesion_exp.usuario_id)
        for r in sesion_exp.resultados:
            resultados.append({
                'usuario': usuario.nombre_usuario if usuario else 'desconocido',
                'sesion_id': sesion_exp.id,
                'grupo': sesion_exp.grupo,
                'especimen': r.especie_id,
                'correcta': r.especie_correcta,
                'seleccionada': r.especie_seleccionada,
                'acerto': r.es_correcta,
                'tiempo': r.tiempo_segundos,
                'tiempo_reflexion': r.tiempo_reflexion_segundos,
                'primera_pregunta_desvio': r.primera_pregunta_desvio,
                'ayuda_encuadre_usos': r.ayuda_encuadre_usos or 0,
                'ayuda_encuadre_detalle': r.ayuda_encuadre_detalle,
                'orden': r.orden
            })
    return jsonify(resultados)

@app.route('/api/admin/encuestas', methods=['GET'])
@admin_required
def get_encuestas():
    try:
        encuestas = []
        for enc in ResultadoEncuesta.query.all():
            sesion_exp = SesionExperimental.query.get(enc.sesion_id)
            if sesion_exp and sesion_exp.anulada:
                continue
            grupo = sesion_exp.grupo if sesion_exp else None

            sus_total = None
            carga_intrinseca = None
            carga_extrinseca = None
            carga_germana = None

            if enc.respuestas_json:
                try:
                    parsed = json.loads(enc.respuestas_json)
                    if enc.tipo == 'SUS':
                        sus_total = calcular_sus(parsed)
                    elif enc.tipo == 'COGNITIVE_LOAD':
                        if isinstance(parsed, list) and len(parsed) >= 10:
                            carga_intrinseca = sum(parsed[0:4])
                            carga_extrinseca = sum(parsed[4:8])
                            carga_germana = sum(parsed[8:10])
                        elif isinstance(parsed, dict):
                            carga_intrinseca = parsed.get('carga_intrinseca')
                            carga_extrinseca = parsed.get('carga_extrinseca')
                            carga_germana = parsed.get('carga_germana')
                except Exception:
                    pass

            encuestas.append({
                'id': enc.id,
                'usuario': sesion_exp.usuario_rel.nombre_usuario if sesion_exp and sesion_exp.usuario_rel else None,
                'sesion_id': enc.sesion_id,
                'tipo': enc.tipo,
                'grupo': grupo,
                'puntaje_total': enc.puntaje_total,
                'sus_total': sus_total,
                'carga_intrinseca': carga_intrinseca,
                'carga_extrinseca': carga_extrinseca,
                'carga_germana': carga_germana,
            })
        return jsonify(encuestas)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/reflexiones', methods=['GET'])
@admin_required
def get_reflexiones():
    try:
        reflexiones = []
        for ref in ReflexionMetacognitiva.query.all():
            sesion_exp = SesionExperimental.query.get(ref.sesion_id)
            if not sesion_exp or sesion_exp.anulada:
                continue
            usuario = Usuario.query.get(sesion_exp.usuario_id)
            if not usuario:
                continue

            respuesta = ref.respuesta or ''
            datos = {
                'id': ref.id,
                'usuario': usuario.nombre_usuario,
                'grupo': sesion_exp.grupo,
                'momento': ref.momento,
                'pregunta': ref.pregunta,
                'respuesta_raw': respuesta,
                'orden_especimen': ref.orden_especimen,
                'timestamp': ref.timestamp.isoformat() if ref.timestamp else None
            }

            numeros = re.findall(r'(\d+)%', respuesta)
            for i, num in enumerate(numeros[:3], 1):
                datos[f'valor{i}'] = int(num)

            reflexiones.append(datos)
        return jsonify(reflexiones)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/pasos', methods=['GET'])
@admin_required
def get_pasos_admin():
    """Devuelve el recorrido detallado de TODOS los estudiantes por la
    clave dicotómica: pregunta, opción, acierto del paso, tiempo y
    orden. Admite filtros opcionales por query string:
    ?usuario=<nombre_usuario>  y/o  ?grupo=<2D|2D_META|3D|3D_META>
    Usado en la sección de Reflexiones del panel admin para revisar
    si un estudiante respondió a conciencia o solo dio clic rápido."""
    try:
        filtro_usuario = request.args.get('usuario')
        filtro_grupo = request.args.get('grupo')

        pasos = []
        for sesion_exp in SesionExperimental.query.filter(SESION_VIGENTE).all():
            if filtro_grupo and sesion_exp.grupo != filtro_grupo:
                continue
            usuario = Usuario.query.get(sesion_exp.usuario_id)
            nombre_usuario = usuario.nombre_usuario if usuario else 'desconocido'
            if filtro_usuario and nombre_usuario != filtro_usuario:
                continue
            for p in sesion_exp.pasos:
                pasos.append({
                    'usuario': nombre_usuario,
                    'grupo': sesion_exp.grupo,
                    'sesion_id': sesion_exp.id,
                    'especimen_id': p.especimen_id,
                    'especimen_orden': p.especimen_orden,
                    'pregunta': p.pregunta,
                    'opcion_elegida': p.opcion_elegida,
                    'es_paso_correcto': p.es_paso_correcto,
                    'es_paso_final': p.es_paso_final,
                    'tiempo_segundos': p.tiempo_segundos,
                    'orden_paso': p.orden_paso,
                    'timestamp': p.timestamp.isoformat() if p.timestamp else None
                })
        return jsonify(pasos)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/usuarios/<int:usuario_id>', methods=['DELETE'])
@admin_required
def eliminar_usuario(usuario_id):
    try:
        usuario = Usuario.query.get(usuario_id)
        if not usuario:
            return jsonify({'error': 'Usuario no encontrado'}), 404
        for sesion_exp in usuario.sesiones:
            PasoClave.query.filter_by(sesion_id=sesion_exp.id).delete()
            ReflexionMetacognitiva.query.filter_by(sesion_id=sesion_exp.id).delete()
            ResultadoEncuesta.query.filter_by(sesion_id=sesion_exp.id).delete()
            ResultadoIdentificacion.query.filter_by(sesion_id=sesion_exp.id).delete()
        SesionExperimental.query.filter_by(usuario_id=usuario_id).delete()
        db.session.delete(usuario)
        db.session.commit()
        return jsonify({'mensaje': 'Usuario eliminado correctamente'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/usuarios/<int:usuario_id>/resetear_password', methods=['POST'])
@admin_required
def resetear_password(usuario_id):
    """Permite al admin poner una CLAVE NUEVA a un usuario que la olvidó.
    Nunca revela la contraseña original (está encriptada con hash de una
    sola vía, es imposible verla). El admin define la nueva clave en el
    panel y se la comunica al estudiante por su cuenta."""
    try:
        usuario = Usuario.query.get(usuario_id)
        if not usuario:
            return jsonify({'error': 'Usuario no encontrado'}), 404
        data = request.json
        nueva_password = (data or {}).get('nueva_password', '').strip()
        if not nueva_password or len(nueva_password) < 4:
            return jsonify({'error': 'La nueva contraseña debe tener al menos 4 caracteres'}), 400
        usuario.contrasena_hash = generate_password_hash(nueva_password)
        db.session.commit()
        return jsonify({'mensaje': f'Contraseña de {usuario.nombre_usuario} actualizada correctamente'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

def _describir_intento(usuario):
    """Estado del intento vigente de un participante, para que el administrador decida con información."""
    codigo, ultima = estado_ejercicio(usuario)
    anulados = [s for s in usuario.sesiones if s.anulada]
    base = {'usuario': usuario.nombre_usuario, 'rol': usuario.rol, 'grupo': usuario.grupo_asignado, 'estado': codigo, 'intentos_anulados': len(anulados)}
    if ultima is None:
        return {**base, 'sesion_id': None, 'resultados': 0, 'pasos': 0, 'encuestas': [], 'inicio': '', 'especies': []}
    try:
        especies = json.loads(ultima.especies_asignadas or '[]')
    except Exception:
        especies = []
    return {**base, 'sesion_id': ultima.id, 'resultados': len({r.orden for r in ultima.resultados}), 'pasos': PasoClave.query.filter_by(sesion_id=ultima.id).count(),
            'encuestas': sorted({e.tipo for e in ultima.encuestas}), 'inicio': (ultima.fecha_inicio + _dt.timedelta(hours=-5)).strftime('%Y-%m-%d %H:%M') if ultima.fecha_inicio else '', 'especies': especies}

@app.route('/api/admin/usuarios/<int:usuario_id>/estado_intento', methods=['GET'])
@admin_required
def estado_intento(usuario_id):
    usuario = db.session.get(Usuario, usuario_id)
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    return jsonify(_describir_intento(usuario))

@app.route('/api/admin/usuarios/<int:usuario_id>/anular_intento', methods=['POST'])
@admin_required
def anular_intento(usuario_id):
    """ANULA el intento vigente de un participante y le habilita uno nuevo. No borra nada: el intento anulado se conserva con todos sus
    datos, marcado con el motivo, y deja de contar en las estadísticas. Si ya tenía identificaciones hechas exige confirmación explícita."""
    try:
        usuario = db.session.get(Usuario, usuario_id)
        if not usuario:
            return jsonify({'error': 'Usuario no encontrado'}), 404
        data = request.json or {}
        info = _describir_intento(usuario)
        if info['sesion_id'] is None:
            return jsonify({'error': 'Esta persona no tiene ningún intento vigente que anular: ya puede iniciar el ejercicio.'}), 400
        if info['resultados'] >= 1 and not data.get('confirmar'):
            return jsonify({'error': 'Este intento ya tiene identificaciones hechas (la persona vio las respuestas correctas). Confirma que quieres anularlo.', 'codigo': 'requiere_confirmacion', **info}), 409
        motivo = (data.get('motivo') or 'No especificado').strip()
        detalle = (data.get('detalle') or '').strip()
        ses = db.session.get(SesionExperimental, info['sesion_id'])
        anular_sesion(ses, motivo + (': ' + detalle if detalle else ''), 'administrador')
        db.session.commit()
        return jsonify({'mensaje': f'Intento de {usuario.nombre_usuario} anulado ({motivo}). Ya puede ingresar de nuevo; el intento anulado se conservó con todos sus datos.', 'anulado': info})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/usuarios/<int:usuario_id>/habilitar_reintento', methods=['POST'])
@admin_required
def habilitar_reintento(usuario_id):
    """Ruta heredada (botón 🔓 de versiones anteriores del panel): anula el intento si está INCOMPLETO. Ya no borra datos."""
    usuario = db.session.get(Usuario, usuario_id)
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    info = _describir_intento(usuario)
    if info['sesion_id'] is None or info['estado'] in ('completado', 'encuestas_pendientes'):
        return jsonify({'error': 'No hay un intento incompleto para habilitar. Usa «Anular y habilitar» para un intento ya completo.'}), 400
    ses = db.session.get(SesionExperimental, info['sesion_id'])
    anular_sesion(ses, 'Cerró la ventana a mitad del ejercicio', 'administrador')
    db.session.commit()
    return jsonify({'mensaje': f'Intento incompleto de {usuario.nombre_usuario} anulado (se conservaron sus datos). Ya puede volver a ingresar.'})


@app.route('/api/admin/reparar_esquema', methods=['GET', 'POST'])
@admin_required
def reparar_esquema():
    """Diagnóstico y reparación: completa las columnas que falten y muestra a qué base está conectada la app."""
    try:
        from sqlalchemy import inspect
        agregadas, errores = asegurar_esquema()
        insp = inspect(db.engine)
        url = db.engine.url
        faltan = {}
        for tabla, columnas in COLUMNAS_NUEVAS.items():
            existentes = {c['name'] for c in insp.get_columns(tabla)} if insp.has_table(tabla) else set()
            faltan[tabla] = [n for n, _ in columnas if n not in existentes] if insp.has_table(tabla) else ['(la tabla no existe)']
        return jsonify({
            'ok': not errores and not any(faltan.values()),
            'base_de_datos': {'tipo': db.engine.dialect.name, 'servidor': url.host, 'nombre': url.database, 'usuario': url.username, 'conexion': MODO_POOL},
            'columnas_agregadas_ahora': agregadas,
            'errores': errores,
            'columnas_que_aun_faltan': faltan,
            'tablas': sorted(insp.get_table_names())
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/admin/rotacion/psicometria', methods=['GET'])
@admin_required
def psicometria_rotacion():
    """Fiabilidad (KR-20), dificultad y discriminación de la prueba de rotación mental con los datos reales.
    Por defecto solo estudiantes; con ?todos=1 se incluyen todos los roles. Los ítems no alcanzados cuentan como incorrectos."""
    import math
    consulta = Usuario.query if request.args.get('todos') == '1' else Usuario.query.filter_by(rol='estudiante')
    filas, no_alcanzo, por_tiempo, total_s = [], 0, 0, []
    for u in consulta.all():
        d = rotacion_items_dict(u)
        if not d: continue
        por_i = {x['i']: x for x in d['items']}
        filas.append([por_i[n]['ok'] if n in por_i else 0 for n in range(1, 13)])
        no_alcanzo += 1 if d.get('alcanzados', 12) < 12 else 0
        por_tiempo += 1 if d.get('por_tiempo') else 0
        total_s.append(d.get('total_s', 0))
    n, k = len(filas), 12
    if n < 3:
        return jsonify({'n': n, 'aviso': 'Todavía hay muy pocos participantes con el detalle de la prueba (se necesitan al menos 3; para una estimación estable, 30 o más).'})
    totales = [sum(f) for f in filas]
    media = sum(totales) / n
    var = lambda xs: (sum((x - sum(xs) / len(xs)) ** 2 for x in xs) / (len(xs) - 1)) if len(xs) > 1 else 0.0
    var_total = var(totales)
    items = []
    suma_var_items = 0.0
    for j in range(k):
        col = [f[j] for f in filas]; p = sum(col) / n; suma_var_items += var(col)
        resto = [t - c for t, c in zip(totales, col)]                 # puntaje sin este ítem (discriminación corregida)
        sx, sy = var(col), var(resto)
        cov = sum((c - p) * (r - sum(resto) / n) for c, r in zip(col, resto)) / (n - 1)
        rpb = cov / math.sqrt(sx * sy) if sx > 0 and sy > 0 else None
        items.append({'item': j + 1, 'dificultad_p': round(p, 3), 'discriminacion_rpb': None if rpb is None else round(rpb, 3)})
    alfa = (k / (k - 1)) * (1 - suma_var_items / var_total) if var_total > 0 else None
    aviso = None
    if n < 30: aviso = f'Con {n} participantes la estimación es inestable: úsala solo como orientación hasta tener al menos 30.'
    return jsonify({'n': n, 'k': k, 'media': round(media, 2), 'desviacion': round(math.sqrt(var_total), 2),
                    'kr20': None if alfa is None else round(alfa, 3), 'items': items,
                    'pct_no_alcanzo_12_items': round(100 * no_alcanzo / n, 1), 'pct_termino_por_tiempo': round(100 * por_tiempo / n, 1),
                    'tiempo_medio_s': round(sum(total_s) / n, 1), 'aviso': aviso})

# ===== EXPORTACIÓN COMPLETA: PAQUETE DE ANÁLISIS =====
# Las tablas se construyen en el servidor, desde la base de datos, CADA VEZ que se piden: así siempre están al día (el panel antes exportaba
# una copia en memoria hecha al abrirlo) y traen TODO lo que la plataforma guarda. De aquí salen el Excel completo, el JSON y el ZIP de CSV.
# Los archivos de análisis son ANÓNIMOS (llevan id_participante); nombres y correos van aparte, solo si se piden.
import zipfile as _zipfile
import csv as _csv
from io import StringIO as _StringIO, BytesIO as _BytesIO

def _hora_col(dt):
    """datetime guardado en UTC (sin zona) -> texto en hora de Colombia (UTC-5, sin horario de verano)."""
    return (dt + _dt.timedelta(hours=-5)).strftime('%Y-%m-%d %H:%M:%S') if dt else ''

def _num_en(txt, patron):
    m = re.search(patron, txt or '')
    return float(m.group(1)) if m else None

def _texto_en(txt, clave):
    m = re.search(clave + r':\s*([^|]+?)\s*(?:\||$)', txt or '')
    return m.group(1).strip() if m else None

def _items(respuestas_json, n):
    """Lista de n respuestas de una encuesta (o n valores vacíos si no hay datos válidos)."""
    try:
        d = json.loads(respuestas_json) if respuestas_json else None
        if isinstance(d, dict): d = list(d.values())
        if isinstance(d, list) and len(d) >= n:
            return [(int(x) if float(x).is_integer() else float(x)) if x not in (None, '') else None for x in d[:n]]
    except Exception:
        pass
    return [None] * n

def _definiciones_tablas():
    rot_desc = ([(f'rot_i{n}', f'Prueba de rotación mental, ítem {n}: 1 = acertó, 0 = falló; vacío = no llegó a verlo') for n in range(1, 13)] +
                [(f'rot_t{n}', f'Prueba de rotación mental, segundos que estuvo en el ítem {n}') for n in range(1, 13)] +
                [('rot_alcanzados', 'Prueba de rotación mental: cuántos de los 12 ítems llegó a ver'), ('rot_total_s', 'Prueba de rotación mental: segundos totales usados (máximo 180)'),
                 ('rot_por_tiempo', 'Prueba de rotación mental: 1 = terminó porque se acabó el tiempo, 0 = terminó por su cuenta')])
    sus_desc = [(f'sus_i{n}', f'SUS, ítem {n} (respuesta de 1 a 5; los ítems pares están formulados en sentido inverso)') for n in range(1, 11)]
    ccp_desc = [(f'ccp_i{n}', f'Carga cognitiva, ítem {n} (0 a 10)') for n in range(1, 11)]
    base_user = [('rol', 'Tipo de participante: estudiante, docente, validador o pruebas'), ('grupo', 'Grupo asignado: 2D, 2D_META, 3D o 3D_META (vacío si no tiene)')]
    covar = [('semestre', 'Semestre que cursa'), ('genero', 'Género declarado en el registro'), ('experiencia_taxonomica', 'Experiencia previa en taxonomía (autoinforme, 1 a 5)'),
             ('conocimiento_genero', 'Conocimiento previo del género Linepithema (autoinforme, 1 a 5)'), ('habilidad_espacial_autoinforme', 'Habilidad espacial percibida (autoinforme, 0 a 20)'),
             ('familiaridad_3d', 'Familiaridad con tecnologías 3D (autoinforme, 1 a 5)'), ('puntaje_rotacion_mental', 'Aciertos en la prueba de rotación mental del registro (0 a 12)')]
    sesion_app = [('sesion_aplicacion', 'Número de la sesión de aplicación (código) con la que entró al estudio; vacío si no escribió ningún código'),
                  ('fecha_sesion', 'Fecha (hora de Colombia) en que se abrió esa sesión de aplicación')]
    id_ = ('id_participante', 'Número interno del participante; sirve para unir todas las tablas')
    return {
        'participantes': [id_] + base_user + [covar[0], covar[1], ('institucion', 'Institución según el dominio del correo'), ('fecha_registro', 'Fecha y hora del registro (hora de Colombia)')] + sesion_app + covar[2:] + rot_desc + [
            ('n_sesiones', 'Cuántas sesiones (intentos) VIGENTES tiene en la plataforma'), ('n_sesiones_anuladas', 'Cuántos intentos suyos fueron anulados (se conservan marcados; no entran al análisis)'), ('n_especimenes', 'Cuántos especímenes distintos identificó'),
            ('aciertos_primer_intento', 'Aciertos contando solo el primer intento de cada espécimen'), ('precision_primer_intento_pct', 'Porcentaje de acierto contando solo el primer intento de cada espécimen'),
            ('tiempo_total_clave_s', 'Segundos en la clave sumando el primer intento de cada espécimen'), ('sus_total', 'Puntaje SUS de usabilidad (0 a 100)'),
            ('ccp_intrinseca', 'Carga cognitiva intrínseca: suma de los ítems 1 a 4 (0 a 40)'), ('ccp_extrinseca', 'Carga cognitiva extrínseca: suma de los ítems 5 a 8 (0 a 40)'),
            ('ccp_germana', 'Carga cognitiva pertinente o germana: suma de los ítems 9 y 10 (0 a 20)'), ('n_cambios_estrategia', 'Cuántas veces eligió cambiar de estrategia'),
            ('comentario_final', 'Comentario final escrito por el participante'), ('primera_sesion_inicio', 'Inicio de su primera sesión (hora de Colombia)'), ('ultima_sesion_fin', 'Fin de su última sesión (hora de Colombia)')],
        'especimenes': [id_] + base_user + [covar[0], covar[1]] + sesion_app + [
            ('sesion_id', 'Número de la sesión (intento) en la base de datos'), ('intento_sesion', 'Orden de esta sesión entre las del participante (1 = primera)'),
            ('orden_especimen', 'Posición del espécimen dentro de la sesión (1 o 2)'), ('especimen_id', 'Identificador del espécimen presentado (especie_número)'),
            ('especie_correcta', 'Especie correcta del espécimen'), ('especie_seleccionada', 'Especie a la que llegó el participante con la clave'), ('acierto', '1 = acertó, 0 = se equivocó'),
            ('es_primer_intento_especimen', '1 = es la primera vez que este participante identifica este espécimen (usar para el análisis principal)'),
            ('registro_repetido', '1 = el mismo espécimen aparece más de una vez para este participante (por un reinicio)'),
            ('tiempo_clave_s', 'Segundos en la clave, sin contar el tiempo de las reflexiones'), ('tiempo_reflexion_s', 'Segundos dedicados a las reflexiones metacognitivas'),
            ('primera_pregunta_desvio', 'Primer pareado en el que se apartó de la ruta correcta; vacío si siguió la ruta correcta'),
            ('ayuda_usos', 'Cuántas veces usó la ayuda «¿Dónde mirar?»'), ('ayuda_detalle', 'Rasgos consultados con la ayuda y el segundo en que los consultó (rasgo@segundo)'),
            ('n_pasos', 'Pasos (pareados) recorridos en este espécimen, sin contar los deshechos'), ('n_pasos_incorrectos', 'Pasos NO deshechos que se apartaron de la ruta correcta'),
            ('post_acierto_percibido', 'Acierto que creyó haber tenido, tras el espécimen (0 a 100; solo grupos con metacognición)'), ('post_dificultad', 'Dificultad percibida (0 a 100)'),
            ('post_esfuerzo', 'Esfuerzo mental percibido (0 a 100)'), ('post_sin_mover', '1 = los tres deslizadores quedaron exactamente en 50. En los datos ANTERIORES a que fueran obligatorios, casi seguro sin mover; después, puede ser una respuesta deliberada'),
            ('seguridad_media_durante', 'Seguridad media declarada durante la clave (0 a 100)'), ('n_cambios_estrategia', 'Cambios de estrategia en este espécimen'),
            ('tiempo_corto', '1 = tardó menos de 15 segundos (posible respuesta al azar)'),
            ('n_pasos_deshechos', 'Pasos que el participante deshizo al volver atrás en este espécimen (no cuentan en n_pasos)'),
            ('sesion_anulada', '1 = este intento fue ANULADO (se conserva pero NO debe entrar al análisis)'), ('motivo_anulacion', 'Motivo de la anulación'),
            ('reintento_tras_anulacion', '1 = intento vigente de una persona que antes tuvo un intento anulado (vio otras especies y quizá sus respuestas)')] + covar[2:] + [
            ('inicio_sesion', 'Inicio de la sesión (hora de Colombia)'), ('fin_sesion', 'Fin de la sesión (hora de Colombia)'), ('sus_total', 'Puntaje SUS del participante (0 a 100)'),
            ('ccp_intrinseca', 'Carga cognitiva intrínseca (suma 0 a 40)'), ('ccp_extrinseca', 'Carga cognitiva extrínseca (suma 0 a 40)'), ('ccp_germana', 'Carga cognitiva germana (suma 0 a 20)')],
        'pasos': [id_, ('rol', 'Tipo de participante'), ('grupo', 'Grupo de la sesión'), ('sesion_id', 'Número de la sesión'), ('intento_sesion', 'Orden de la sesión entre las del participante'),
                  ('especimen_id', 'Espécimen'), ('orden_especimen', 'Posición del espécimen en la sesión'), ('pregunta', 'Número de pareado'), ('opcion_elegida', 'Opción elegida (A o B)'),
                  ('es_paso_correcto', '1 = siguió la ruta correcta'), ('es_paso_final', '1 = este paso llevó a una especie'), ('tiempo_segundos', 'Segundos en este paso'),
                  ('orden_paso', 'Orden del paso dentro de la sesión (continúa tras un reinicio)'), ('momento', 'Fecha y hora del paso (hora de Colombia)'),
                  ('deshecho', '1 = el participante volvió atrás y deshizo este paso'), ('sesion_anulada', '1 = pertenece a un intento anulado')],
        'encuestas': [id_, ('rol', 'Tipo de participante'), ('grupo', 'Grupo de la sesión'), ('sesion_id', 'Número de la sesión')] + sus_desc + [('sus_total', 'Puntaje SUS (0 a 100)')] + ccp_desc + [
            ('ccp_intrinseca', 'Suma de los ítems 1 a 4 (0 a 40)'), ('ccp_extrinseca', 'Suma de los ítems 5 a 8 (0 a 40)'), ('ccp_germana', 'Suma de los ítems 9 y 10 (0 a 20)'),
            ('fecha_sus', 'Cuándo guardó el SUS (hora de Colombia)'), ('fecha_ccp', 'Cuándo guardó la carga cognitiva (hora de Colombia)'), ('sesion_anulada', '1 = pertenece a un intento anulado')],
        'reflexiones': [id_, ('rol', 'Tipo de participante'), ('grupo', 'Grupo de la sesión'), ('sesion_id', 'Número de la sesión'), ('momento', 'pre, durante, post o comentario_final'),
                        ('orden_especimen', 'Espécimen al que se refiere (0 = antes de empezar)'), ('momento_hora', 'Fecha y hora (hora de Colombia)'), ('texto', 'Texto completo guardado'),
                        ('nivel_conocimiento', 'PRE: nivel de conocimiento percibido (0 a 100)'), ('confianza', 'PRE: confianza (0 a 100)'), ('razonamiento_espacial', 'PRE: razonamiento espacial percibido (0 a 100)'),
                        ('estrategia', 'PRE: estrategia elegida'), ('seguridad', 'DURANTE: seguridad (0 a 100)'), ('cambia_estrategia', 'DURANTE: 1 = eligió cambiar de estrategia'),
                        ('nueva_estrategia', 'DURANTE: estrategia nueva elegida'), ('acierto_percibido', 'POST: acierto percibido (0 a 100)'), ('dificultad', 'POST: dificultad percibida (0 a 100)'),
                        ('esfuerzo', 'POST: esfuerzo mental percibido (0 a 100)'), ('sesion_anulada', '1 = pertenece a un intento anulado')],
        'comentarios': [id_, ('rol', 'Tipo de participante'), ('grupo', 'Grupo'), ('semestre', 'Semestre'), ('sesion_aplicacion', 'Sesión de aplicación'), ('comentario_final', 'Comentario final escrito')],
    }

def construir_tablas_exportacion(solo_estudiantes=False, identidades=False):
    defs = _definiciones_tablas()
    usuarios = [u for u in Usuario.query.order_by(Usuario.id).all() if (u.rol == 'estudiante' or not solo_estudiantes)]
    por_uid, uids = {u.id: u for u in usuarios}, {u.id for u in usuarios}
    sesiones = [s for s in SesionExperimental.query.order_by(SesionExperimental.id).all() if s.usuario_id in uids]
    sids, ses_de_user, intento = {s.id for s in sesiones}, {}, {}
    for s in sesiones:
        ses_de_user.setdefault(s.usuario_id, []).append(s); intento[s.id] = len(ses_de_user[s.usuario_id])
    res_de, pas_de, enc_de, ref_de = {}, {}, {}, {}
    for r in ResultadoIdentificacion.query.order_by(ResultadoIdentificacion.id).all():
        if r.sesion_id in sids: res_de.setdefault(r.sesion_id, []).append(r)
    for p in PasoClave.query.order_by(PasoClave.id).all():
        if p.sesion_id in sids: pas_de.setdefault(p.sesion_id, []).append(p)
    for e in ResultadoEncuesta.query.order_by(ResultadoEncuesta.id).all():
        if e.sesion_id in sids: enc_de.setdefault(e.sesion_id, []).append(e)
    for x in ReflexionMetacognitiva.query.order_by(ReflexionMetacognitiva.id).all():
        if x.sesion_id in sids: ref_de.setdefault(x.sesion_id, []).append(x)
    fechas_ses = {s['n']: (_a_fecha(s['inicio']) + _dt.timedelta(hours=-5)).strftime('%Y-%m-%d') for s in _leer_sesiones()}
    ses_por_id = {s.id: s for s in sesiones}

    def encuestas_de(s):
        enc = enc_de.get(s.id, [])
        sus = next((e for e in enc if e.tipo == 'SUS'), None); ccp = next((e for e in enc if e.tipo == 'COGNITIVE_LOAD'), None)
        sus_i, ccp_i = (_items(sus.respuestas_json, 10) if sus else [None] * 10), (_items(ccp.respuestas_json, 10) if ccp else [None] * 10)
        try: sus_total = calcular_sus(json.loads(sus.respuestas_json)) if sus and sus.respuestas_json else None
        except Exception: sus_total = None
        suma = lambda v: sum(v) if all(x is not None for x in v) else None
        return {'sus_i': sus_i, 'sus_total': sus_total, 'ccp_i': ccp_i, 'ccp_intrinseca': suma(ccp_i[0:4]), 'ccp_extrinseca': suma(ccp_i[4:8]), 'ccp_germana': suma(ccp_i[8:10]),
                'fecha_sus': _hora_col(sus.fecha) if sus else '', 'fecha_ccp': _hora_col(ccp.fecha) if ccp else ''}

    base = lambda u: {'id_participante': u.id, 'rol': u.rol, 'grupo': u.grupo_asignado or '', 'semestre': u.semestre, 'genero': u.genero, 'sesion_aplicacion': u.sesion_aplicacion,
                      'fecha_sesion': fechas_ses.get(u.sesion_aplicacion, ''), 'experiencia_taxonomica': u.experiencia_taxonomica, 'conocimiento_genero': u.conocimiento_genero,
                      'habilidad_espacial_autoinforme': u.habilidad_espacial, 'familiaridad_3d': u.familiaridad_3d, 'puntaje_rotacion_mental': u.puntaje_rotacion_mental}
    t_part, t_esp, t_pas, t_enc, t_ref, t_com = [], [], [], [], [], []
    for u in usuarios:
        ses_u = ses_de_user.get(u.id, [])
        ses_v = [s for s in ses_u if not s.anulada]                    # intentos VIGENTES: de ellos salen los datos agregados de la persona
        res_u = [r for s in ses_v for r in res_de.get(s.id, [])]
        vistos, primeros = set(), []
        for r in res_u:
            if r.especie_id not in vistos: vistos.add(r.especie_id); primeros.append(r)
        ac1 = sum(1 for r in primeros if r.es_correcta)
        enc_u = {'sus_total': None, 'ccp_intrinseca': None, 'ccp_extrinseca': None, 'ccp_germana': None}
        for s in ses_v:                                              # la primera encuesta de cada tipo que haya
            e = encuestas_de(s)
            for k in enc_u:
                if enc_u[k] is None: enc_u[k] = e[k]
        refs_u = [x for s in ses_v for x in ref_de.get(s.id, [])]
        cambios = sum(1 for x in refs_u if x.momento == 'durante' and re.search(r'¿Cambiar estrategia\?\s*S[ií]', x.respuesta or ''))
        coment = [x.respuesta for x in refs_u if x.momento == 'comentario_final' and (x.respuesta or '').strip()]
        coment = coment[-1].strip() if coment else ''
        rot = dict(zip(ROT_COLUMNAS, columnas_rotacion(u)))
        fila = {**base(u), 'institucion': u.institucion or u.curso or '', 'fecha_registro': _hora_col(u.fecha_registro), **rot, 'n_sesiones': len(ses_v), 'n_sesiones_anuladas': len(ses_u) - len(ses_v), 'n_especimenes': len(vistos),
                'aciertos_primer_intento': ac1 if primeros else '', 'precision_primer_intento_pct': round(100 * ac1 / len(primeros), 1) if primeros else '',
                'tiempo_total_clave_s': round(sum(r.tiempo_segundos or 0 for r in primeros), 1) if primeros else '', **enc_u, 'n_cambios_estrategia': cambios, 'comentario_final': coment,
                'primera_sesion_inicio': _hora_col(ses_v[0].fecha_inicio) if ses_v else '', 'ultima_sesion_fin': _hora_col(ses_v[-1].fecha_fin) if ses_v else ''}
        t_part.append([fila.get(c, '') for c, _ in defs['participantes']])
        t_com.append([u.id, u.rol, u.grupo_asignado or '', u.semestre, u.sesion_aplicacion, coment]) if coment else None
        ya = {}
        for s in ses_u:
            anul = 1 if s.anulada else 0
            hubo_anulado_antes = any(q.anulada and q.id < s.id for q in ses_u)
            e = encuestas_de(s); refs = ref_de.get(s.id, []); pasos_s = pas_de.get(s.id, [])
            fila_enc = {'id_participante': u.id, 'rol': u.rol, 'grupo': s.grupo or '', 'sesion_id': s.id, **{f'sus_i{n + 1}': v for n, v in enumerate(e['sus_i'])}, 'sus_total': e['sus_total'],
                        **{f'ccp_i{n + 1}': v for n, v in enumerate(e['ccp_i'])}, **{k: e[k] for k in ('ccp_intrinseca', 'ccp_extrinseca', 'ccp_germana', 'fecha_sus', 'fecha_ccp')}, 'sesion_anulada': anul}
            if enc_de.get(s.id): t_enc.append([fila_enc.get(c, '') for c, _ in defs['encuestas']])
            for x in refs:
                t = x.respuesta or ''
                fr = {'id_participante': u.id, 'rol': u.rol, 'grupo': s.grupo or '', 'sesion_id': s.id, 'sesion_anulada': anul, 'momento': x.momento, 'orden_especimen': x.orden_especimen, 'momento_hora': _hora_col(x.timestamp), 'texto': t,
                      'nivel_conocimiento': _num_en(t, r'Nivel de conocimiento:\s*(\d+(?:\.\d+)?)'), 'confianza': _num_en(t, r'Confianza:\s*(\d+(?:\.\d+)?)'),
                      'razonamiento_espacial': _num_en(t, r'Razonamiento espacial:\s*(\d+(?:\.\d+)?)'), 'estrategia': _texto_en(t, 'Estrategia'),
                      'seguridad': _num_en(t, r'Seguridad:\s*(\d+(?:\.\d+)?)'), 'nueva_estrategia': _texto_en(t, 'Nueva estrategia'),
                      'acierto_percibido': _num_en(t, r'Acierto autopercibido:\s*(\d+(?:\.\d+)?)'), 'dificultad': _num_en(t, r'Dificultad:\s*(\d+(?:\.\d+)?)'), 'esfuerzo': _num_en(t, r'Esfuerzo mental:\s*(\d+(?:\.\d+)?)')}
                if x.momento == 'durante': fr['cambia_estrategia'] = 1 if re.search(r'¿Cambiar estrategia\?\s*S[ií]', t) else 0
                t_ref.append([fr.get(c, '') if fr.get(c) is not None else '' for c, _ in defs['reflexiones']])
            for p in pasos_s:
                fp = {'id_participante': u.id, 'rol': u.rol, 'grupo': s.grupo or '', 'sesion_id': s.id, 'intento_sesion': intento[s.id], 'especimen_id': p.especimen_id, 'orden_especimen': p.especimen_orden,
                      'pregunta': p.pregunta, 'opcion_elegida': p.opcion_elegida, 'es_paso_correcto': 1 if p.es_paso_correcto else 0, 'es_paso_final': 1 if p.es_paso_final else 0,
                      'tiempo_segundos': p.tiempo_segundos, 'orden_paso': p.orden_paso, 'momento': _hora_col(p.timestamp), 'deshecho': 1 if p.deshecho else 0, 'sesion_anulada': anul}
                t_pas.append([fp.get(c, '') if fp.get(c) is not None else '' for c, _ in defs['pasos']])
            for r in res_de.get(s.id, []):
                if not anul:
                    ya[r.especie_id] = ya.get(r.especie_id, 0) + 1
                veces = 0 if anul else sum(1 for q in res_u if q.especie_id == r.especie_id)
                post = [x for x in refs if x.momento == 'post' and x.orden_especimen == r.orden]
                pt = post[-1].respuesta if post else ''
                ap, di, es = _num_en(pt, r'Acierto autopercibido:\s*(\d+(?:\.\d+)?)'), _num_en(pt, r'Dificultad:\s*(\d+(?:\.\d+)?)'), _num_en(pt, r'Esfuerzo mental:\s*(\d+(?:\.\d+)?)')
                dur = [x for x in refs if x.momento == 'durante' and x.orden_especimen == r.orden]
                seg = [v for v in (_num_en(x.respuesta, r'Seguridad:\s*(\d+(?:\.\d+)?)') for x in dur) if v is not None]
                pasos_mios = [p for p in pasos_s if p.especimen_id == r.especie_id and (p.especimen_orden in (None, r.orden))]
                pasos_e = [p for p in pasos_mios if not p.deshecho]                       # los pasos deshechos no cuentan como recorrido
                fe = {**base(u), 'sesion_id': s.id, 'intento_sesion': intento[s.id], 'orden_especimen': r.orden, 'especimen_id': r.especie_id, 'especie_correcta': r.especie_correcta,
                      'especie_seleccionada': r.especie_seleccionada, 'acierto': 1 if r.es_correcta else 0, 'es_primer_intento_especimen': 0 if anul else (1 if ya[r.especie_id] == 1 and r.id == next(q.id for q in res_u if q.especie_id == r.especie_id) else 0),
                      'registro_repetido': 1 if veces > 1 else 0, 'tiempo_clave_s': r.tiempo_segundos, 'tiempo_reflexion_s': r.tiempo_reflexion_segundos, 'primera_pregunta_desvio': r.primera_pregunta_desvio,
                      'ayuda_usos': r.ayuda_encuadre_usos or 0, 'ayuda_detalle': r.ayuda_encuadre_detalle or '', 'n_pasos': len(pasos_e), 'n_pasos_incorrectos': sum(1 for p in pasos_e if not p.es_paso_correcto),
                      'post_acierto_percibido': ap, 'post_dificultad': di, 'post_esfuerzo': es, 'post_sin_mover': (1 if (ap == di == es == 50) else 0) if post else '',
                      'seguridad_media_durante': round(sum(seg) / len(seg), 1) if seg else None, 'n_cambios_estrategia': sum(1 for x in dur if re.search(r'¿Cambiar estrategia\?\s*S[ií]', x.respuesta or '')) if dur else '',
                      'tiempo_corto': 1 if (r.tiempo_segundos is not None and r.tiempo_segundos < 15) else 0, 'n_pasos_deshechos': sum(1 for p in pasos_mios if p.deshecho),
                      'sesion_anulada': anul, 'motivo_anulacion': (s.motivo_anulacion or '') if anul else '', 'reintento_tras_anulacion': 1 if (not anul and hubo_anulado_antes) else 0, 'inicio_sesion': _hora_col(s.fecha_inicio), 'fin_sesion': _hora_col(s.fecha_fin),
                      'sus_total': enc_u['sus_total'], 'ccp_intrinseca': enc_u['ccp_intrinseca'], 'ccp_extrinseca': enc_u['ccp_extrinseca'], 'ccp_germana': enc_u['ccp_germana']}
                t_esp.append([fe.get(c, '') if fe.get(c) is not None else '' for c, _ in defs['especimenes']])
    tablas = {'participantes': t_part, 'especimenes': t_esp, 'pasos': t_pas, 'encuestas': t_enc, 'reflexiones': t_ref, 'comentarios': t_com}
    # los vacíos de la tabla de participantes también deben ser '' y no None
    tablas['participantes'] = [['' if v is None else v for v in f] for f in tablas['participantes']]
    salida = {n: {'columnas': [c for c, _ in defs[n]], 'filas': tablas[n]} for n in tablas}
    salida['diccionario_variables'] = {'columnas': ['tabla', 'variable', 'descripcion'], 'filas': [[n, c, d] for n in defs for c, d in defs[n]]}
    if identidades:
        salida['identidades'] = {'columnas': ['id_participante', 'nombre', 'correo'], 'filas': [[u.id, u.nombre_usuario, u.correo] for u in usuarios]}
    return salida

def _csv_bytes(columnas, filas):
    out = _StringIO(); w = _csv.writer(out); w.writerow(columnas)
    for f in filas: w.writerow(['' if v is None else v for v in f])
    return ('\ufeff' + out.getvalue()).encode('utf-8')          # con BOM para que Excel respete las tildes

def _leeme(tablas, solo_estudiantes, identidades):
    ahora = _hora_col(_dt.datetime.utcnow())
    cuerpo = '\n'.join(f"  {n}.csv — {len(t['filas'])} filas" for n, t in tablas.items())
    return f'''PAQUETE DE ANÁLISIS — Clave Linepithema
Generado: {ahora} (hora de Colombia)
Contenido: {'solo estudiantes' if solo_estudiantes else 'todos los tipos de participante (usa la columna «rol» para filtrar; el análisis principal usa rol = estudiante)'}
{'Incluye identidades.csv (nombres y correos): guárdalo aparte y no lo compartas.' if identidades else 'Archivos ANÓNIMOS: los participantes se identifican con id_participante (sin nombres ni correos).'}

ARCHIVOS
{cuerpo}

CÓMO UNIRLOS
  Todas las tablas comparten id_participante. «especimenes» tiene una fila por espécimen identificado (formato largo, listo para R o SPSS).
  «participantes» tiene una fila por persona. «pasos», «encuestas», «reflexiones» y «comentarios» son el detalle.
  diccionario_variables.csv describe CADA columna de CADA tabla.
  analisis.R (si está incluido) reproduce los modelos principales en R: ponlo junto a los CSV y ejecútalo.

CONVENCIONES
  - Codificación UTF-8 con BOM; separador coma; decimales con punto; celda vacía = dato no disponible.
  - Fechas y horas en hora de Colombia (UTC-5).
  - Para el análisis principal usa es_primer_intento_especimen = 1 (descarta los reinicios que repitieron un espécimen).
  - Revisa tiempo_corto y post_sin_mover como señales de calidad de datos.
'''

@app.route('/api/admin/analisis', methods=['GET'])
@admin_required
def analisis_estadistico():
    """Análisis estadístico del estudio (solo estudiantes). Se calcula en el momento, con los datos actuales.
    ?repetidos=1 incluye todos los intentos (por defecto, solo el primer intento de cada espécimen); ?permutaciones=N (500-10000)."""
    if estadistica is None:
        return jsonify({'error': 'Falta el archivo backend/estadistica.py en el servidor. Súbelo a GitHub junto con app.py.', 'codigo': 'falta_estadistica'}), 503
    try:
        try:
            B = max(500, min(10000, int(request.args.get('permutaciones', 3000))))
        except ValueError:
            B = 3000
        res = estadistica.analizar(construir_tablas_exportacion(True, False), B=B, incluir_repetidos=request.args.get('repetidos') == '1')
        res['generado'] = _hora_col(_dt.datetime.utcnow())
        return jsonify(res)
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/monitor_sesion', methods=['GET'])
@admin_required
def monitor_sesion():
    """Monitor en vivo de la sesión de aplicación: en qué punto del ejercicio va cada estudiante y quién lleva rato sin avanzar.
    Incluye a quienes entraron con el código vigente y a quienes tuvieron actividad en las últimas ?horas= (por defecto 6)."""
    try:
        try:
            horas = max(1, min(72, int(request.args.get('horas', 6))))
        except ValueError:
            horas = 6
        ahora = _dt.datetime.utcnow()
        limite = ahora - _dt.timedelta(hours=horas)
        lista = _leer_sesiones()
        n_actual = lista[-1]['n'] if lista else None
        ult_ses = {}
        for s in SesionExperimental.query.filter(SESION_VIGENTE).order_by(SesionExperimental.id).all():
            ult_ses[s.usuario_id] = s                                                      # la última sesión vigente de cada persona
        anuladas = dict(db.session.query(SesionExperimental.usuario_id, func.count(SesionExperimental.id)).filter(SesionExperimental.anulada.is_(True)).group_by(SesionExperimental.usuario_id).all())
        ids = [s.id for s in ult_ses.values()]
        pasos, ordenes, tipos, refs = {}, {}, {}, {}
        if ids:
            for p in PasoClave.query.filter(PasoClave.sesion_id.in_(ids)).order_by(PasoClave.id).all():
                d = pasos.setdefault(p.sesion_id, {'ts': None, 'ultimo': None})
                if p.timestamp and (d['ts'] is None or p.timestamp > d['ts']): d['ts'] = p.timestamp
                if not p.deshecho: d['ultimo'] = p
            for r in ResultadoIdentificacion.query.filter(ResultadoIdentificacion.sesion_id.in_(ids)).all():
                ordenes.setdefault(r.sesion_id, set()).add(r.orden)
            for e in ResultadoEncuesta.query.filter(ResultadoEncuesta.sesion_id.in_(ids)).all():
                d = tipos.setdefault(e.sesion_id, {'tipos': set(), 'ts': None}); d['tipos'].add(e.tipo)
                if e.fecha and (d['ts'] is None or e.fecha > d['ts']): d['ts'] = e.fecha
            for x in ReflexionMetacognitiva.query.filter(ReflexionMetacognitiva.sesion_id.in_(ids)).all():
                d = refs.setdefault(x.sesion_id, {'ts': None, 'comentario': False})
                if x.timestamp and (d['ts'] is None or x.timestamp > d['ts']): d['ts'] = x.timestamp
                if x.momento == 'comentario_final' and (x.respuesta or '').strip(): d['comentario'] = True
        filas = []
        for u in Usuario.query.filter_by(rol='estudiante').all():
            s = ult_ses.get(u.id)
            marcas = [t for t in (u.fecha_registro, s.fecha_inicio if s else None, pasos.get(s.id, {}).get('ts') if s else None, tipos.get(s.id, {}).get('ts') if s else None, refs.get(s.id, {}).get('ts') if s else None) if t]
            actividad = max(marcas) if marcas else None
            if not (u.sesion_aplicacion == n_actual and n_actual is not None) and not (actividad and actividad >= limite):
                continue
            n_res = len(ordenes.get(s.id, set())) if s else 0
            if not u.grupo_asignado:
                estado, detalle = 'sin_grupo', 'Se registró sin el código: fuera del estudio'
            elif s is None:
                estado, detalle = 'sin_iniciar', 'Registrado; todavía no inicia el ejercicio'
            elif n_res < 2:
                ultimo = pasos.get(s.id, {}).get('ultimo')
                estado = 'en_clave'
                detalle = f'Espécimen {n_res + 1} de 2' + (f' · va en el pareado {ultimo.pregunta}' if ultimo is not None and ultimo.especimen_orden == n_res + 1 else ' · aún sin responder')
            else:
                tp = tipos.get(s.id, {}).get('tipos', set())
                if not {'SUS', 'COGNITIVE_LOAD'} <= tp:
                    estado, detalle = 'encuestas', f"Encuestas: SUS {'✓' if 'SUS' in tp else '✗'} · Carga cognitiva {'✓' if 'COGNITIVE_LOAD' in tp else '✗'}"
                elif not refs.get(s.id, {}).get('comentario'):
                    estado, detalle = 'comentario', 'Falta el comentario final'
                else:
                    estado, detalle = 'terminado', 'Terminó todo'
            minutos = round((ahora - actividad).total_seconds() / 60, 1) if actividad else None
            umbral = (8, 15) if estado == 'sin_iniciar' else (4, 8)
            # Una alerta sirve para actuar EN CLASE: pasados 90 min sin actividad se asume que la persona se fue y deja de ser alerta (se muestra atenuado)
            alerta = 0 if estado in ('terminado', 'sin_grupo') or minutos is None or minutos > 90 else (2 if minutos >= umbral[1] else (1 if minutos >= umbral[0] else 0))
            filas.append({'id': u.id, 'nombre': u.nombre_usuario, 'semestre': u.semestre, 'grupo': u.grupo_asignado, 'estado': estado, 'detalle': detalle, 'identificaciones': n_res,
                          'minutos_inactivo': minutos, 'ultima_actividad': _hora_col(actividad)[11:16] if actividad else '', 'alerta': alerta, 'intentos_anulados': anuladas.get(u.id, 0), 'sesion_aplicacion': u.sesion_aplicacion})
        orden = {'en_clave': 0, 'encuestas': 0, 'comentario': 0, 'sin_iniciar': 1, 'sin_grupo': 2, 'terminado': 3}
        filas.sort(key=lambda f: (orden[f['estado']], -(f['alerta']), -(f['minutos_inactivo'] or 0) if f['estado'] != 'terminado' else (f['minutos_inactivo'] or 0)))
        resumen = {k: sum(1 for f in filas if f['estado'] == k) for k in orden}
        resumen['alertas'] = sum(1 for f in filas if f['alerta'] > 0)
        resumen['total'] = len(filas)
        return jsonify({'filas': filas, 'resumen': resumen, 'horas': horas, 'sesion_aplicacion': n_actual, 'generado': _hora_col(ahora)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/tablas_exportacion', methods=['GET'])
@admin_required
def tablas_exportacion():
    """Las tablas del paquete de análisis en JSON (siempre frescas): con ellas el panel arma el Excel completo y el JSON."""
    try:
        t = construir_tablas_exportacion(request.args.get('solo_estudiantes') == '1', request.args.get('identidades') == '1')
        return jsonify({'generado': _hora_col(_dt.datetime.utcnow()), 'conteos': {n: len(x['filas']) for n, x in t.items()}, 'tablas': t})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/paquete_datos', methods=['GET'])
@admin_required
def paquete_datos():
    """ZIP con las tablas en CSV, el diccionario de variables y un LEEME. ?identidades=1 agrega nombres y correos; ?solo_estudiantes=1 filtra."""
    try:
        solo, ident = request.args.get('solo_estudiantes') == '1', request.args.get('identidades') == '1'
        t = construir_tablas_exportacion(solo, ident)
        buf = _BytesIO()
        with _zipfile.ZipFile(buf, 'w', _zipfile.ZIP_DEFLATED) as z:
            z.writestr('LEEME.txt', _leeme(t, solo, ident).encode('utf-8'))
            if estadistica is not None:
                z.writestr('analisis.R', estadistica.SCRIPT_R.encode('utf-8'))
            for n, x in t.items():
                z.writestr(n + '.csv', _csv_bytes(x['columnas'], x['filas']))
        nombre = 'paquete_analisis' + ('_con_identidades' if ident else '') + '_' + _hora_col(_dt.datetime.utcnow())[:10] + '.zip'
        return Response(buf.getvalue(), mimetype='application/zip', headers={'Content-Disposition': f'attachment; filename={nombre}'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/exportar_csv', methods=['GET'])
@admin_required
def exportar_csv():
    """Exporta UNA FILA POR ESPÉCIMEN (ítem) identificado, lista para R/SPSS.

    Columnas del ítem: participante, grupo, institución, especie, orden, acierto, tiempo en clave,
    tiempo en reflexión, primera pregunta de desvío, confianza, dificultad, esfuerzo.
    Extras útiles: seguridad media y cambios de estrategia durante la clave, variables de línea base,
    SUS y carga cognitiva (por participante, repetidos en sus 2 filas) y un control de duplicados."""
    from io import StringIO
    import csv
    from flask import Response

    def numero(txt, patron):
        m = re.search(patron, txt or '')
        return float(m.group(1)) if m else None

    columnas = [
        'participante', 'grupo', 'rol', 'institucion', 'sesion_aplicacion', 'fecha_sesion', 'sesion_id', 'especie', 'orden', 'acierto',
        'tiempo_clave_s', 'tiempo_reflexion_s', 'primera_pregunta_desvio', 'ayuda_encuadre_usos', 'ayuda_encuadre_rasgos',
        'confianza', 'dificultad', 'esfuerzo',
        'seguridad_media_durante', 'n_cambios_estrategia', 'registros_duplicados',
        'semestre', 'experiencia_taxonomica', 'conocimiento_genero',
        'habilidad_espacial_autoinforme', 'familiaridad_3d', 'puntaje_rotacion_mental', *ROT_COLUMNAS,
        'sus', 'carga_intrinseca', 'carga_extrinseca', 'carga_germana'
    ]
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(columnas)

    fechas_sesion = {s['n']: (_a_fecha(s['inicio']) + _dt.timedelta(hours=-5)).strftime('%Y-%m-%d') for s in _leer_sesiones()}        # fecha en que se abrió cada sesión de aplicación, en hora de Colombia (UTC-5, sin horario de verano)
    filas = []
    for sesion_exp in SesionExperimental.query.filter(SESION_VIGENTE).all():
        usuario = Usuario.query.get(sesion_exp.usuario_id)
        if not usuario:
            continue

        # Encuestas de la sesión (se toma la primera de cada tipo)
        sus = ci = ce = cg = None
        vistos = set()
        for enc in sorted(sesion_exp.encuestas, key=lambda x: x.id):
            if enc.tipo in vistos or not enc.respuestas_json:
                continue
            vistos.add(enc.tipo)
            try:
                parsed = json.loads(enc.respuestas_json)
                if enc.tipo == 'SUS':
                    sus = calcular_sus(parsed)
                elif enc.tipo == 'COGNITIVE_LOAD' and isinstance(parsed, list) and len(parsed) >= 10:
                    ci, ce, cg = sum(parsed[0:4]), sum(parsed[4:8]), sum(parsed[8:10])
            except Exception:
                pass

        # Reflexiones por espécimen (orden_especimen; en datos viejos, se lee de "espécimen N")
        post, durante = {}, {}
        for ref in sorted(sesion_exp.reflexiones, key=lambda x: x.id):
            orden = ref.orden_especimen
            if orden is None and ref.momento == 'post':
                m = re.search(r'espécimen (\d+)', ref.pregunta or '')
                orden = int(m.group(1)) if m else None
            if orden is None:
                continue
            if ref.momento == 'post' and orden not in post:
                post[orden] = ref.respuesta or ''
            elif ref.momento == 'durante':
                durante.setdefault(orden, []).append(ref.respuesta or '')

        # Resultados: una fila por orden; si hubo registros repetidos se cuenta y se deja el primero
        por_orden = {}
        for r in sorted(sesion_exp.resultados, key=lambda x: x.id):
            por_orden.setdefault(r.orden, []).append(r)

        for orden, lista in por_orden.items():
            r = lista[0]
            seg = [numero(t, r'Seguridad:\s*(\d+(?:\.\d+)?)') for t in durante.get(orden, [])]
            seg = [x for x in seg if x is not None]
            cambios = sum(1 for t in durante.get(orden, []) if re.search(r'¿Cambiar estrategia\?\s*S[ií]', t))
            tp = post.get(orden)
            filas.append([
                usuario.nombre_usuario, sesion_exp.grupo, usuario.rol, usuario.institucion or usuario.curso, usuario.sesion_aplicacion, fechas_sesion.get(usuario.sesion_aplicacion, ''), sesion_exp.id,
                r.especie_correcta, orden, 1 if r.es_correcta else 0,
                r.tiempo_segundos, r.tiempo_reflexion_segundos, r.primera_pregunta_desvio, r.ayuda_encuadre_usos or 0, r.ayuda_encuadre_detalle or '',
                numero(tp, r'Acierto autopercibido:\s*(\d+(?:\.\d+)?)'),
                numero(tp, r'Dificultad:\s*(\d+(?:\.\d+)?)'),
                numero(tp, r'Esfuerzo mental:\s*(\d+(?:\.\d+)?)'),
                round(sum(seg) / len(seg), 1) if seg else None,
                cambios if durante.get(orden) else None,
                len(lista) - 1,
                usuario.semestre, usuario.experiencia_taxonomica, usuario.conocimiento_genero,
                usuario.habilidad_espacial, usuario.familiaridad_3d, usuario.puntaje_rotacion_mental, *columnas_rotacion(usuario),
                sus, ci, ce, cg
            ])

    filas.sort(key=lambda f: (str(f[0]), f[5] or 0))
    writer.writerows(filas)

    # BOM para que Excel abra bien las tildes
    return Response(
        '\ufeff' + output.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=items_linepithema.csv'}
    )

# ===== FOTOS 2D DE LOS ESPECÍMENES, SERVIDAS POR CÓDIGO =====
# La clave las pide como /foto/esp2/lateral.jpg para que el nombre de la especie no aparezca en la dirección.
# Se sirven desde la carpeta con el código (esp2) si existe y, si no, desde la carpeta con el nombre de la especie,
# así no hace falta renombrar carpetas en el repositorio.
CODIGO_A_ESPECIE = {'esp1': 'gallardoi', 'esp2': 'humile', 'esp3': 'tsachila', 'esp4': 'piliferum', 'esp5': 'hirsutum',
                    'esp6': 'angulatum', 'esp7': 'neotropicum', 'esp8': 'dispertitum', 'esp9': 'iniquum'}
VISTAS_FOTO = ('lateral', 'cabeza', 'dorsal')

@app.route('/foto/<codigo>/<vista>.jpg')
def foto_especimen(codigo, vista):
    if codigo not in CODIGO_A_ESPECIE or vista not in VISTAS_FOTO:
        return jsonify({'error': 'No encontrada'}), 404
    base = os.path.join(app.static_folder, 'imagenes', 'linepithema')
    for carpeta in (codigo, CODIGO_A_ESPECIE[codigo]):
        if os.path.isfile(os.path.join(base, carpeta, vista + '.jpg')):
            return send_from_directory(os.path.join(base, carpeta), vista + '.jpg', max_age=3600)
    return jsonify({'error': 'No encontrada'}), 404

# ===== API EXPERIMENTO =====
def estado_ejercicio(usuario):
    """sin_iniciar | abandonado | encuestas_pendientes | completado (según la última sesión del usuario)."""
    sesiones = sorted(sesiones_validas(usuario), key=lambda s: s.id)       # los intentos anulados no cuentan
    if not sesiones:
        return 'sin_iniciar', None
    ultima = sesiones[-1]
    if len({r.orden for r in ultima.resultados}) >= 2:
        tipos = {e.tipo for e in ultima.encuestas}
        return ('completado' if {'SUS', 'COGNITIVE_LOAD'} <= tipos else 'encuestas_pendientes'), ultima
    return 'abandonado', ultima

@app.route('/api/experimento/deshacer_paso', methods=['POST'])
def deshacer_paso():
    """El participante volvió atrás: se MARCA el paso como deshecho (no se borra; así se puede analizar cuántas veces rectificó)."""
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    try:
        d = request.json or {}
        ses = db.session.get(SesionExperimental, d.get('sesion_id'))
        if not ses or ses.usuario_id != session['usuario_id']:
            return jsonify({'error': 'Sesión no válida'}), 403
        paso = (PasoClave.query.filter_by(sesion_id=ses.id, especimen_id=d.get('especimen_id'), especimen_orden=d.get('especimen_orden'), orden_paso=d.get('orden_paso'))
                .order_by(PasoClave.id.desc()).first())
        if not paso:
            return jsonify({'error': 'Paso no encontrado'}), 404
        paso.deshecho = True
        db.session.commit()
        return jsonify({'mensaje': 'Paso deshecho'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/experimento/estado', methods=['GET'])
def estado_experimento():
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    usuario = Usuario.query.get(session['usuario_id'])
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    codigo, ultima_ses = estado_ejercicio(usuario)
    libre = usuario.rol == 'pruebas'   # las cuentas de pruebas pueden repetir el ejercicio
    sin_nada = codigo == 'abandonado' and ultima_ses is not None and not ultima_ses.resultados     # abrió el ejercicio y se fue sin completar ninguna identificación
    return jsonify({
        'codigo': 'sin_iniciar' if sin_nada else codigo,
        'rol': usuario.rol,
        'grupo': usuario.grupo_asignado,
        'puede_iniciar': libre or codigo == 'sin_iniciar' or sin_nada,
        'reanudar_encuestas': (not libre) and codigo == 'encuestas_pendientes'
    })

def especies_vistas(usuario):
    """Especies que ya le tocaron en intentos ANULADOS: en un intento nuevo se evitan (ya las vio, y quizá vio la respuesta)."""
    vistas = set()
    for s in usuario.sesiones:
        if s.anulada:
            try:
                vistas.update(json.loads(s.especies_asignadas or '[]'))
            except Exception:
                pass
    return vistas

def elegir_especies_balanceadas(grupo, candidatas):
    """Las 2 especies MENOS presentadas hasta ahora en ese grupo (entre estudiantes, intentos vigentes); los empates se deciden al azar.
    Así cada especie se reparte parejo dentro de cada grupo (la dificultad varía mucho entre especies) y no se repiten las mismas parejas."""
    cuenta = {e: 0 for e in candidatas}
    filas = (db.session.query(SesionExperimental.especies_asignadas).join(Usuario, Usuario.id == SesionExperimental.usuario_id)
             .filter(Usuario.rol == 'estudiante', SesionExperimental.grupo == grupo, SESION_VIGENTE).all())
    for (txt,) in filas:
        try:
            for e in json.loads(txt or '[]'):
                if e in cuenta:
                    cuenta[e] += 1
        except Exception:
            pass
    barajadas = list(candidatas)
    random.shuffle(barajadas)
    barajadas.sort(key=lambda e: cuenta[e])          # orden estable: el azar previo desempata
    return barajadas[:2]

@app.route('/api/experimento/iniciar', methods=['POST'])
def iniciar_experimento():
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    try:
        data = request.json
        usuario_id = session['usuario_id']
        usuario = Usuario.query.get(usuario_id)
        if not usuario:
            return jsonify({'error': 'Usuario no encontrado'}), 404

        # Un estudiante solo participa si el código de la sesión le dio grupo (el botón del panel ya lo impide; aquí se exige también en el servidor)
        if usuario.rol == 'estudiante' and not usuario.grupo_asignado:
            return jsonify({'error': 'Aún no tienes grupo asignado: escribe el código de la sesión de aplicación en tu panel.', 'codigo': 'sin_grupo'}), 403

        # ===== UN SOLO INTENTO por participante (las cuentas de pruebas quedan libres) =====
        if usuario.rol != 'pruebas':
            codigo, ultima = estado_ejercicio(usuario)
            if codigo == 'encuestas_pendientes':
                return jsonify({
                    'sesion_id': ultima.id,
                    'especimenes': json.loads(ultima.especimenes_asignados or '[]'),
                    'grupo': usuario.grupo_asignado,
                    'reanudar': 'encuestas'
                })
            if codigo == 'abandonado' and ultima is not None and not ultima.resultados:
                # Abrió el ejercicio y se fue sin completar ninguna identificación: se libera solo (el intento queda anulado, con sus datos)
                anular_sesion(ultima, 'Abandonó sin completar ninguna identificación (liberado automáticamente)', 'automático')
                db.session.commit()
                codigo = 'sin_iniciar'
            if codigo != 'sin_iniciar':
                return jsonify({'error': 'Ya realizaste este ejercicio y solo puede hacerse una vez. Si cerraste la ventana por error, avisa al investigador.',
                                'codigo': 'ya_realizado'}), 409

        # ===== FIX Fase 4: garantizar 2 especies DISTINTAS por sesión =====
        especies_activas = get_especies_activas()
        if len(especies_activas) < 2:
            return jsonify({'error': 'No hay suficientes especies activas'}), 400

        candidatas = [e for e in especies_activas if e not in especies_vistas(usuario)]      # en un intento nuevo, evitar las que ya vio
        if len(candidatas) < 2:
            candidatas = list(especies_activas)
        if usuario.rol == 'estudiante':
            bloquear_asignacion()                                                              # candado: dos inicios simultáneos no ven el mismo conteo
            especies_elegidas = elegir_especies_balanceadas(usuario.grupo_asignado, candidatas)
        else:
            especies_elegidas = random.sample(candidatas, 2)
        especimenes = []
        for especie in especies_elegidas:
            numero = random.choice([1, 2])
            especimenes.append({'id': f'{especie}_{numero}', 'especie': especie})
        # ===== FIN FIX =====

        nueva_sesion = SesionExperimental(
            usuario_id=usuario.id,
            grupo=usuario.grupo_asignado,
            especies_asignadas=json.dumps([e['especie'] for e in especimenes]),
            especimenes_asignados=json.dumps(especimenes)
        )
        db.session.add(nueva_sesion)
        db.session.commit()
        return jsonify({
            'sesion_id': nueva_sesion.id,
            'especimenes': especimenes,
            'grupo': usuario.grupo_asignado
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/experimento/guardar_resultado', methods=['POST'])
def guardar_resultado():
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    try:
        data = request.json
        resultado = ResultadoIdentificacion(
            sesion_id=data['sesion_id'],
            especie_id=data['especimen_id'],
            especie_correcta=data['especie_correcta'],
            especie_seleccionada=data['especie_seleccionada'],
            es_correcta=data['es_correcta'],
            tiempo_segundos=data['tiempo_segundos'],
            tiempo_reflexion_segundos=data.get('tiempo_reflexion_segundos'),
            primera_pregunta_desvio=data.get('primera_pregunta_desvio'),
            ayuda_encuadre_usos=int(data.get('ayuda_encuadre_usos') or 0),
            ayuda_encuadre_detalle=(str(data.get('ayuda_encuadre_detalle'))[:500] if data.get('ayuda_encuadre_detalle') else None),
            orden=data['orden']
        )
        db.session.add(resultado)
        db.session.commit()
        return jsonify({'mensaje': 'Resultado guardado'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/experimento/guardar_paso', methods=['POST'])
def guardar_paso():
    """Registra cada clic del estudiante navegando la clave dicotómica:
    pregunta, opción elegida, tiempo empleado, y si coincidía con el
    camino correcto. Usado para el detalle de en qué falló y con qué
    tiempo/consciencia respondió (dashboards de estudiante y admin)."""
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    try:
        data = request.json
        paso = PasoClave(
            sesion_id=data['sesion_id'],
            especimen_id=data['especimen_id'],
            especimen_orden=data.get('especimen_orden'),
            pregunta=data['pregunta'],
            opcion_elegida=data['opcion_elegida'],
            es_paso_correcto=data['es_paso_correcto'],
            es_paso_final=data.get('es_paso_final', False),
            tiempo_segundos=data['tiempo_segundos'],
            orden_paso=data['orden_paso']
        )
        db.session.add(paso)
        db.session.commit()
        return jsonify({'mensaje': 'Paso guardado'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/experimento/guardar_reflexion', methods=['POST'])
def guardar_reflexion():
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    try:
        data = request.json
        reflexion = ReflexionMetacognitiva(
            sesion_id=data['sesion_id'],
            momento=data['momento'],
            pregunta=data['pregunta'],
            respuesta=data['respuesta'],
            orden_especimen=data.get('orden_especimen')
        )
        db.session.add(reflexion)
        db.session.commit()
        return jsonify({'mensaje': 'Reflexión guardada'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/experimento/guardar_encuesta', methods=['POST'])
def guardar_encuesta():
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    try:
        data = request.json
        respuestas = data['respuestas']
        tipo = data['tipo']

        puntaje_total = data.get('puntaje_total')
        if tipo == 'SUS' and not puntaje_total:
            puntaje_total = calcular_sus(respuestas)

        encuesta = ResultadoEncuesta(
            sesion_id=data['sesion_id'],
            tipo=tipo,
            respuestas_json=json.dumps(respuestas),
            puntaje_total=puntaje_total
        )
        db.session.add(encuesta)
        db.session.commit()
        respuesta = {'mensaje': 'Encuesta guardada', 'puntaje': puntaje_total}
        if tipo == 'COGNITIVE_LOAD':
            siguiente = avanzar_grupo_pruebas(session['usuario_id'], data['sesion_id'])
            if siguiente:
                respuesta['siguiente_grupo'] = siguiente       # solo cuentas de pruebas
        return jsonify(respuesta)
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# ===== ARRANQUE =====
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        set_especies_activas([e['id'] for e in POOL_ESPECIES if e['activa']])
    app.run(debug=False, port=5000)
