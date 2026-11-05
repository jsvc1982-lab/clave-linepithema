from flask import Flask, request, jsonify, session, send_from_directory, redirect, url_for
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
import random
import json
import os
import re
import time
from sqlalchemy import func, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, DisconnectionError
from sqlalchemy.pool import NullPool

try:
    from backend.models import db, Usuario, SesionExperimental, ResultadoIdentificacion, ResultadoEncuesta, ReflexionMetacognitiva, Configuracion, PasoClave
except ImportError:
    from models import db, Usuario, SesionExperimental, ResultadoIdentificacion, ResultadoEncuesta, ReflexionMetacognitiva, Configuracion, PasoClave

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
    'usuarios': [('conocimiento_genero', 'INTEGER DEFAULT 1'), ('puntaje_rotacion_mental', 'INTEGER'), ('institucion', 'VARCHAR(80)'), ('rotacion_items', 'TEXT')],
    'resultados': [('tiempo_reflexion_segundos', 'DOUBLE PRECISION'), ('primera_pregunta_desvio', 'INTEGER'),
                   ('ayuda_encuadre_usos', 'INTEGER DEFAULT 0'), ('ayuda_encuadre_detalle', 'TEXT')],
    'reflexiones': [('orden_especimen', 'INTEGER')],
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
        db.session.add(nuevo_usuario)
        db.session.commit()
        return jsonify({'mensaje': 'Registro exitoso', 'id': nuevo_usuario.id}), 201
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

def sesion_visible_pruebas(usuario):
    """Intento más reciente que ya tiene al menos un resultado: es el que muestra el panel (los anteriores quedan como historial)."""
    for s in sorted(usuario.sesiones, key=lambda x: x.id, reverse=True):
        if s.resultados:
            return s
    return None

def sesiones_visibles(usuario):
    if usuario.rol == 'pruebas':
        s = sesion_visible_pruebas(usuario)
        return [s] if s else []
    return usuario.sesiones

def progreso_pruebas(usuario):
    if usuario.rol != 'pruebas':
        return None
    hechos = []
    for s in sorted(usuario.sesiones, key=lambda x: x.id):
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
        ).order_by(SesionExperimental.id.desc()).first()
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
    for sesion_exp in usuario.sesiones:
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
        ).order_by(SesionExperimental.id.desc()).first()

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
    sesiones_por_usuario = dict(db.session.query(SesionExperimental.usuario_id, func.count(SesionExperimental.id)).group_by(SesionExperimental.usuario_id).all())
    return jsonify([{
        'id': u.id,
        'n_sesiones': sesiones_por_usuario.get(u.id, 0),
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
        'grupo_asignado': u.grupo_asignado,
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
        sesiones = SesionExperimental.query.filter_by(usuario_id=usuario.id).count()
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

@app.route('/api/admin/asignar_grupo_lote', methods=['POST'])
@admin_required
def asignar_grupo_lote():
    """Asigna grupos automáticamente en rotación a usuarios sin grupo."""
    try:
        usuarios_sin_grupo = Usuario.query.filter_by(grupo_asignado=None).all()
        grupos = ['2D', '2D_META', '3D', '3D_META']
        asignados = 0
        for i, u in enumerate(usuarios_sin_grupo):
            u.grupo_asignado = grupos[i % len(grupos)]
            asignados += 1
        db.session.commit()
        return jsonify({'mensaje': f'{asignados} usuarios asignados en rotación'})
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
    for sesion_exp in SesionExperimental.query.all():
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
            if not sesion_exp:
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
        for sesion_exp in SesionExperimental.query.all():
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

@app.route('/api/admin/usuarios/<int:usuario_id>/habilitar_reintento', methods=['POST'])
@admin_required
def habilitar_reintento(usuario_id):
    """Elimina el intento INCOMPLETO de un participante (cerró la ventana a mitad) para que pueda volver a
    ingresar. Un intento con las dos identificaciones completas nunca se borra."""
    try:
        usuario = Usuario.query.get(usuario_id)
        if not usuario:
            return jsonify({'error': 'Usuario no encontrado'}), 404
        borrados = 0
        for s in list(usuario.sesiones):
            if len({r.orden for r in s.resultados}) >= 2:
                continue
            PasoClave.query.filter_by(sesion_id=s.id).delete()
            ReflexionMetacognitiva.query.filter_by(sesion_id=s.id).delete()
            ResultadoEncuesta.query.filter_by(sesion_id=s.id).delete()
            ResultadoIdentificacion.query.filter_by(sesion_id=s.id).delete()
            SesionExperimental.query.filter_by(id=s.id).delete()
            borrados += 1
        db.session.commit()
        if borrados == 0:
            return jsonify({'error': 'No hay un intento incompleto para habilitar. Un intento con las dos identificaciones completas no se borra.'}), 400
        return jsonify({'mensaje': f'Intento incompleto de {usuario.nombre_usuario} eliminado. Ya puede volver a ingresar.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

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
        'participante', 'grupo', 'rol', 'institucion', 'sesion_id', 'especie', 'orden', 'acierto',
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

    filas = []
    for sesion_exp in SesionExperimental.query.all():
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
                usuario.nombre_usuario, sesion_exp.grupo, usuario.rol, usuario.institucion or usuario.curso, sesion_exp.id,
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
    sesiones = sorted(usuario.sesiones, key=lambda s: s.id)
    if not sesiones:
        return 'sin_iniciar', None
    ultima = sesiones[-1]
    if len({r.orden for r in ultima.resultados}) >= 2:
        tipos = {e.tipo for e in ultima.encuestas}
        return ('completado' if {'SUS', 'COGNITIVE_LOAD'} <= tipos else 'encuestas_pendientes'), ultima
    return 'abandonado', ultima

@app.route('/api/experimento/estado', methods=['GET'])
def estado_experimento():
    if not session.get('usuario_id'):
        return jsonify({'error': 'No autenticado'}), 401
    usuario = Usuario.query.get(session['usuario_id'])
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    codigo, _ = estado_ejercicio(usuario)
    libre = usuario.rol == 'pruebas'   # las cuentas de pruebas pueden repetir el ejercicio
    return jsonify({
        'codigo': codigo,
        'rol': usuario.rol,
        'grupo': usuario.grupo_asignado,
        'puede_iniciar': libre or codigo == 'sin_iniciar',
        'reanudar_encuestas': (not libre) and codigo == 'encuestas_pendientes'
    })

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
            if codigo != 'sin_iniciar':
                return jsonify({'error': 'Ya realizaste este ejercicio y solo puede hacerse una vez. Si cerraste la ventana por error, avisa al investigador.',
                                'codigo': 'ya_realizado'}), 409

        # ===== FIX Fase 4: garantizar 2 especies DISTINTAS por sesión =====
        especies_activas = get_especies_activas()
        if len(especies_activas) < 2:
            return jsonify({'error': 'No hay suficientes especies activas'}), 400

        especies_elegidas = random.sample(especies_activas, 2)
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
