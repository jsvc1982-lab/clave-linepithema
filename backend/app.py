from flask import Flask, request, jsonify, session, send_from_directory, redirect, url_for
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
import random
import json
import os
import re

try:
    from backend.models import db, Usuario, SesionExperimental, ResultadoIdentificacion, ResultadoEncuesta, ReflexionMetacognitiva, Configuracion
except ImportError:
    from models import db, Usuario, SesionExperimental, ResultadoIdentificacion, ResultadoEncuesta, ReflexionMetacognitiva, Configuracion

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
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 280,
}

CORS(app, supports_credentials=True, origins=os.getenv('ALLOWED_ORIGIN', 'http://127.0.0.1:5000'))

db.init_app(app)
with app.app_context():
    db.create_all()

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
    {'id': 'iniquum',     'nombre': 'Linepithema iniquum',     'activa': False},
    {'id': 'neotropicum', 'nombre': 'Linepithema neotropicum', 'activa': False},
    {'id': 'hirsutum',    'nombre': 'Linepithema hirsutum',    'activa': False},
    {'id': 'dispertitum', 'nombre': 'Linepithema dispertitum', 'activa': False},
    {'id': 'tsachila',    'nombre': 'Linepithema tsachila',    'activa': False},
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
    config = Configuracion.query.filter_by(clave='especies_activas').first()
    if config:
        return json.loads(config.valor)
    return [e['id'] for e in POOL_ESPECIES if e['activa']]

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

@app.route('/admin')
def serve_admin():
    if session.get('admin_logged_in'):
        return send_from_directory(app.template_folder, 'admin.html')
    return send_from_directory(app.template_folder, 'admin_login.html')

# ===== API USUARIOS (PÚBLICAS) =====
@app.route('/api/registro', methods=['POST'])
def registro():
    try:
        datos = request.json
        if not datos:
            return jsonify({'error': 'Datos inválidos'}), 400

        usuario_val = datos.get('usuario', '').strip()
        correo_val = datos.get('correo', '').strip()
        contrasena_val = datos.get('contrasena', '')

        if not usuario_val or not correo_val or not contrasena_val:
            return jsonify({'error': 'Usuario, correo y contraseña son obligatorios'}), 400

        if Usuario.query.filter_by(nombre_usuario=usuario_val).first():
            return jsonify({'error': 'Usuario ya existe'}), 400
        if Usuario.query.filter_by(correo=correo_val).first():
            return jsonify({'error': 'Correo ya registrado'}), 400

        nuevo_usuario = Usuario(
            nombre_usuario=usuario_val,
            correo=correo_val,
            contrasena_hash=generate_password_hash(contrasena_val),
            semestre=datos.get('semestre'),
            curso=datos.get('curso'),
            genero=datos.get('genero', 'No especificado'),
            experiencia_taxonomica=datos.get('experiencia', 3),
            habilidad_espacial=datos.get('habilidad_espacial', 12),
            familiaridad_3d=datos.get('familiaridad_3d', 3)
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

        usuario = Usuario.query.filter_by(nombre_usuario=datos.get('usuario', '').strip()).first()
        if usuario and check_password_hash(usuario.contrasena_hash, datos.get('contrasena', '')):
            session['usuario_id'] = usuario.id
            session['usuario_nombre'] = usuario.nombre_usuario
            return jsonify({
                'mensaje': 'Login exitoso',
                'usuario': usuario.nombre_usuario,
                'grupo': usuario.grupo_asignado,
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
        'familiaridad_3d': usuario.familiaridad_3d
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
        'familiaridad_3d': usuario.familiaridad_3d
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
        set_especies_activas(activas)
        return jsonify({'mensaje': 'Configuración actualizada'})
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
                'timestamp': ref.timestamp.isoformat() if ref.timestamp else None
            }

            numeros = re.findall(r'(\d+)%', respuesta)
            for i, num in enumerate(numeros[:3], 1):
                datos[f'valor{i}'] = int(num)

            reflexiones.append(datos)
        return jsonify(reflexiones)
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

@app.route('/api/admin/exportar_csv', methods=['GET'])
@admin_required
def exportar_csv():
    """Exporta todos los resultados como CSV."""
    from io import StringIO
    import csv
    from flask import Response

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['usuario', 'grupo', 'sesion_id', 'especimen',
                     'especie_correcta', 'especie_seleccionada', 'acerto', 'tiempo_s'])

    for sesion_exp in SesionExperimental.query.all():
        usuario = Usuario.query.get(sesion_exp.usuario_id)
        nombre = usuario.nombre_usuario if usuario else 'desconocido'
        for r in sesion_exp.resultados:
            writer.writerow([
                nombre, sesion_exp.grupo, sesion_exp.id,
                r.especie_id, r.especie_correcta, r.especie_seleccionada,
                r.es_correcta, r.tiempo_segundos
            ])

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=resultados_linepithema.csv'}
    )

# ===== API EXPERIMENTO =====
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

        pool = get_pool_especimenes()
        if len(pool) < 2:
            return jsonify({'error': 'No hay suficientes especímenes activos'}), 400

        especimenes = random.sample(pool, 2)
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
            orden=data['orden']
        )
        db.session.add(resultado)
        db.session.commit()
        return jsonify({'mensaje': 'Resultado guardado'})
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
            respuesta=data['respuesta']
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
