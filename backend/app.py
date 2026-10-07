from flask import Flask, request, jsonify, session, send_from_directory, redirect, url_for
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
import random
import json
import os
import re
from sqlalchemy import func

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
# Render cierra las conexiones inactivas de la base: se comprueban antes de usarlas y se renuevan cada ~5 minutos.
# Evita errores como "SSL error: decryption failed or bad record mac" tras una pausa o un reinicio de la base.
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 280}

CORS(app, supports_credentials=True, origins=os.getenv('ALLOWED_ORIGIN', 'http://127.0.0.1:5000'))

# ===== ESQUEMA: agrega columnas nuevas a una base creada con una versión anterior =====
# db.create_all() solo crea tablas que NO existen; no agrega columnas a las que ya existen. Por eso, si la base se
# creó (o se restauró de un respaldo) con una versión vieja, esta función completa las columnas que falten.
COLUMNAS_NUEVAS = {
    'usuarios': [('conocimiento_genero', 'INTEGER DEFAULT 1'), ('puntaje_rotacion_mental', 'INTEGER'), ('institucion', 'VARCHAR(80)')],
    'resultados': [('tiempo_reflexion_segundos', 'DOUBLE PRECISION'), ('primera_pregunta_desvio', 'INTEGER')],
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
            puntaje_rotacion_mental=datos.get('puntaje_rotacion_mental')
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
        'puntaje_rotacion_mental': usuario.puntaje_rotacion_mental,
        'rol': usuario.rol,
        'institucion': usuario.institucion,
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
    for sesion_exp in usuario.sesiones:
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
        'puntaje_rotacion_mental': usuario.puntaje_rotacion_mental,
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
    return jsonify([{
        'id': u.id,
        'nombre_usuario': u.nombre_usuario,
        'correo': u.correo,
        'semestre': u.semestre,
        'curso': u.curso,
        'genero': u.genero,
        'experiencia_taxonomica': u.experiencia_taxonomica,
        'habilidad_espacial': u.habilidad_espacial,
        'familiaridad_3d': u.familiaridad_3d,
        'conocimiento_genero': u.conocimiento_genero,
        'puntaje_rotacion_mental': u.puntaje_rotacion_mental,
        'rol': u.rol,
        'institucion': u.institucion,
        'grupo_asignado': u.grupo_asignado,
        'fecha_registro': u.fecha_registro.isoformat() if u.fecha_registro else None
    } for u in usuarios])

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
        usuario.grupo_asignado = data['grupo']
        db.session.commit()
        return jsonify({'mensaje': f'Grupo {data["grupo"]} asignado a {usuario.nombre_usuario}'})
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
            'base_de_datos': {'tipo': db.engine.dialect.name, 'servidor': url.host, 'nombre': url.database, 'usuario': url.username},
            'columnas_agregadas_ahora': agregadas,
            'errores': errores,
            'columnas_que_aun_faltan': faltan,
            'tablas': sorted(insp.get_table_names())
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

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
        'tiempo_clave_s', 'tiempo_reflexion_s', 'primera_pregunta_desvio',
        'confianza', 'dificultad', 'esfuerzo',
        'seguridad_media_durante', 'n_cambios_estrategia', 'registros_duplicados',
        'semestre', 'experiencia_taxonomica', 'conocimiento_genero',
        'habilidad_espacial_autoinforme', 'familiaridad_3d', 'puntaje_rotacion_mental',
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
                r.tiempo_segundos, r.tiempo_reflexion_segundos, r.primera_pregunta_desvio,
                numero(tp, r'Acierto autopercibido:\s*(\d+(?:\.\d+)?)'),
                numero(tp, r'Dificultad:\s*(\d+(?:\.\d+)?)'),
                numero(tp, r'Esfuerzo mental:\s*(\d+(?:\.\d+)?)'),
                round(sum(seg) / len(seg), 1) if seg else None,
                cambios if durante.get(orden) else None,
                len(lista) - 1,
                usuario.semestre, usuario.experiencia_taxonomica, usuario.conocimiento_genero,
                usuario.habilidad_espacial, usuario.familiaridad_3d, usuario.puntaje_rotacion_mental,
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
        return jsonify({'mensaje': 'Encuesta guardada', 'puntaje': puntaje_total})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# ===== ARRANQUE =====
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        set_especies_activas([e['id'] for e in POOL_ESPECIES if e['activa']])
    app.run(debug=False, port=5000)
