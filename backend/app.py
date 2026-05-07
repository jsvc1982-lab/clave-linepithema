from flask import Flask, request, jsonify, session, send_from_directory
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
try:
    from backend.models import db, Usuario, SesionExperimental, ResultadoIdentificacion, ResultadoEncuesta, ReflexionMetacognitiva, Configuracion
except ImportError:
    from models import db, Usuario, SesionExperimental, ResultadoIdentificacion, ResultadoEncuesta, ReflexionMetacognitiva, Configuracion
import random
import json
from datetime import datetime
import os
from functools import wraps
import os
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()
app = Flask(__name__, static_folder='../frontend/static', template_folder='../frontend/templates')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'clave-secreta-tesis-2024')

# Base de datos en memoria (sin problemas de permisos)
import os

# Configurar base de datos para Render (PostgreSQL) o local (SQLite)
database_url = os.getenv('DATABASE_URL', 'sqlite:///database.db')
if database_url and database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

print("📂 Base de datos en memoria (RAM)")

CORS(app, supports_credentials=True, origins="http://127.0.0.1:5000")
db.init_app(app)
with app.app_context():
    db.create_all()
    print("✅ Tablas creadas/verificadas en la base de datos")

# Pool completo de especies (todas las disponibles)
POOL_ESPECIES = [
    {'id': 'humile', 'nombre': 'Linepithema humile', 'activa': True},
    {'id': 'angulatum', 'nombre': 'Linepithema angulatum', 'activa': True},
    {'id': 'piliferum', 'nombre': 'Linepithema piliferum', 'activa': True},
    {'id': 'gallardoi', 'nombre': 'Linepithema gallardoi', 'activa': True},
    {'id': 'iniquum', 'nombre': 'Linepithema iniquum', 'activa': False},
    {'id': 'neotropicum', 'nombre': 'Linepithema neotropicum', 'activa': False},
    {'id': 'hirsutum', 'nombre': 'Linepithema hirsutum', 'activa': False},
    {'id': 'dispertitum', 'nombre': 'Linepithema dispertitum', 'activa': False},
    {'id': 'tsachila', 'nombre': 'Linepithema tsachila', 'activa': False}
]
# ===== CONFIGURACIÓN ADMIN =====
ADMIN_PASSWORD = "admin123"
ADMIN_USER = "admin"

# ===== DECORADOR PARA PROTEGER RUTAS ADMIN =====
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return jsonify({'error': 'Acceso no autorizado'}), 401
        return f(*args, **kwargs)
    return decorated_function

# ===== FUNCIONES DE CONFIGURACIÓN =====
def get_especies_activas():
    """Devuelve la lista de especies activas (las que se usan en las claves)"""
    config = Configuracion.query.filter_by(clave='especies_activas').first()
    if config:
        return json.loads(config.valor)
    # Valor por defecto: primeras 4 especies
    return [e['id'] for e in POOL_ESPECIES[:4] if e['activa']]

def set_especies_activas(especies_ids):
    """Guarda las especies activas en la configuración"""
    config = Configuracion.query.filter_by(clave='especies_activas').first()
    if config:
        config.valor = json.dumps(especies_ids)
    else:
        config = Configuracion(clave='especies_activas', valor=json.dumps(especies_ids))
        db.session.add(config)
    db.session.commit()

def get_pool_especimenes():
    """Devuelve la lista de especímenes basada en las especies activas"""
    especies_activas = get_especies_activas()
    pool = []
    for especie in especies_activas:
        # 2 especímenes por especie activa
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
def serve_dashboard():
    return send_from_directory(app.template_folder, 'dashboard.html')

@app.route('/clave_2d')
def serve_clave_2d():
    return send_from_directory(app.template_folder, 'clave_2d.html')

@app.route('/clave_2d_meta')
def serve_clave_2d_meta():
    return send_from_directory(app.template_folder, 'clave_2d_meta.html')

@app.route('/clave_3d')
def serve_clave_3d():
    return send_from_directory(app.template_folder, 'clave_3d.html')

@app.route('/clave_3d_meta')
def serve_clave_3d_meta():
    return send_from_directory(app.template_folder, 'clave_3d_meta.html')

@app.route('/admin')
def serve_admin():
    if session.get('admin_logged_in'):
        return send_from_directory(app.template_folder, 'admin.html')
    return send_from_directory(app.template_folder, 'admin_login.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('C:/tesis_linepithema/frontend/static', filename)

# ===== API USUARIOS =====
# ===== API PÚBLICAS PARA USUARIOS =====
@app.route('/api/usuario/<nombre_usuario>', methods=['GET'])
def get_usuario_by_nombre(nombre_usuario):
    """Obtiene datos de un usuario por su nombre"""
    usuario = Usuario.query.filter_by(nombre_usuario=nombre_usuario).first()
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

@app.route('/api/usuario/<int:usuario_id>/resultados', methods=['GET'])
def get_resultados_usuario(usuario_id):
    """Obtiene los resultados de identificación de un usuario específico"""
    # Verificar que el usuario existe
    usuario = Usuario.query.get(usuario_id)
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    
    resultados = []
    for sesion in usuario.sesiones:
        for r in sesion.resultados:
            resultados.append({
                'sesion_id': sesion.id,
                'especimen': r.especie_id,
                'correcta': r.especie_correcta,
                'seleccionada': r.especie_seleccionada,
                'acerto': r.es_correcta,
                'tiempo': r.tiempo_segundos,
                'orden': r.orden
            })
    return jsonify(resultados)


@app.route('/api/registro', methods=['POST'])
def registro():
    try:
        datos = request.json
        print(f"📥 Registro: {datos.get('usuario')}")
        
        if Usuario.query.filter_by(nombre_usuario=datos['usuario']).first():
            return jsonify({'error': 'Usuario ya existe'}), 400
        if Usuario.query.filter_by(correo=datos['correo']).first():
            return jsonify({'error': 'Correo ya registrado'}), 400
        
        hashed_password = generate_password_hash(datos['contrasena'])
        nuevo_usuario = Usuario(
            nombre_usuario=datos['usuario'],
            correo=datos['correo'],
            contrasena_hash=hashed_password,
            semestre=datos.get('semestre'),
            curso=datos.get('curso'),
            genero=datos.get('genero', 'No especificado'),
            experiencia_taxonomica=datos.get('experiencia', 3),
            habilidad_espacial=datos.get('habilidad_espacial', 12),
            familiaridad_3d=datos.get('familiaridad_3d', 3)
        )
        db.session.add(nuevo_usuario)
        db.session.commit()
        
        print(f"✅ Usuario creado: {nuevo_usuario.nombre_usuario}")
        return jsonify({'mensaje': 'Registro exitoso', 'id': nuevo_usuario.id}), 201
    except Exception as e:
        print(f"❌ Error en registro: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/login', methods=['POST'])
def login():
    try:
        datos = request.json
        usuario = Usuario.query.filter_by(nombre_usuario=datos['usuario']).first()
        if usuario and check_password_hash(usuario.contrasena_hash, datos['contrasena']):
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
    """Verifica si el usuario tiene sesión activa"""
    if session.get('usuario_id'):
        return jsonify({
            'activa': True,
            'usuario': session.get('usuario_nombre'),
            'id': session.get('usuario_id')
        })
    return jsonify({'activa': False}), 401
# ===== API ADMIN LOGIN =====
@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.json
    password = data.get('password')
    username = data.get('username')
    
    if username == ADMIN_USER and password == ADMIN_PASSWORD:
        session['admin_logged_in'] = True
        session['admin_user'] = username
        return jsonify({'success': True, 'message': 'Login exitoso'})
    elif password == ADMIN_PASSWORD:  # Compatibilidad con versión anterior
        session['admin_logged_in'] = True
        session['admin_user'] = 'admin'
        return jsonify({'success': True, 'message': 'Login exitoso'})
    
    return jsonify({'success': False, 'error': 'Credenciales incorrectas'}), 401
        
    if username:
        # Modo usuario + contraseña
        if username == ADMIN_USER and password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            session['admin_user'] = username
            return jsonify({'success': True, 'message': 'Login exitoso'})
    else:
        # Modo solo contraseña (compatibilidad)
        if password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            session['admin_user'] = 'admin'
            return jsonify({'success': True, 'message': 'Login exitoso'})
    
    return jsonify({'success': False, 'error': 'Credenciales incorrectas'}), 401

@app.route('/api/admin/verificar', methods=['GET'])
def verificar_admin():
    """Verifica si el administrador tiene sesión activa"""
    if session.get('admin_logged_in'):
        return jsonify({
            'authenticated': True,
            'user': session.get('admin_user')
        })
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
        'grupo_asignado': u.grupo_asignado
    } for u in usuarios])

@app.route('/api/admin/asignar_grupo', methods=['POST'])
@admin_required
def asignar_grupo():
    try:
        data = request.json
        usuario = Usuario.query.get(data['usuario_id'])
        if usuario:
            usuario.grupo_asignado = data['grupo']
            db.session.commit()
            return jsonify({'mensaje': f'Grupo {data["grupo"]} asignado'})
        return jsonify({'error': 'Usuario no encontrado'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/config/especies', methods=['GET'])
@admin_required
def get_especies_config():
    especies_activas = get_especies_activas()
    return jsonify({
        'todas': POOL_ESPECIES,
        'activas': especies_activas
    })

@app.route('/api/admin/config/especies', methods=['POST'])
@admin_required
def set_especies_config():
    try:
        data = request.json
        set_especies_activas(data['activas'])
        return jsonify({'mensaje': 'Configuración actualizada'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/datos', methods=['GET'])
@admin_required
def ver_datos():
    """Devuelve todos los resultados de identificación"""
    resultados = []
    for sesion in SesionExperimental.query.all():
        usuario = Usuario.query.get(sesion.usuario_id)
        for r in sesion.resultados:
            resultados.append({
                'usuario': usuario.nombre_usuario if usuario else 'desconocido',
                'sesion_id': sesion.id,
                'grupo': sesion.grupo,
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
    """Devuelve todas las encuestas para análisis SUS y carga cognitiva"""
    try:
        print("🔍 Consultando encuestas...")
        encuestas = []
        for enc in ResultadoEncuesta.query.all():
            sesion = SesionExperimental.query.get(enc.sesion_id)
            grupo = sesion.grupo if sesion else None
            
            # Parsear respuestas JSON - puede ser dict, list, o string
            sus_total = None
            carga_intrinseca = None
            carga_extrinseca = None
            carga_germana = None
            
            if enc.respuestas_json:
                try:
                    parsed = json.loads(enc.respuestas_json)
                    
                    # Si es una lista (formato antiguo)
                    if isinstance(parsed, list):
                        if enc.tipo == 'SUS' and len(parsed) >= 10:
                            # Calcular SUS a partir de la lista
                            score = 0
                            for i in range(10):
                                if i % 2 == 0:
                                    score += parsed[i] - 1
                                else:
                                    score += 5 - parsed[i]
                            sus_total = score * 2.5
                        elif enc.tipo == 'COGNITIVE_LOAD' and len(parsed) >= 10:
                            carga_intrinseca = sum(parsed[0:4])
                            carga_extrinseca = sum(parsed[4:8])
                            carga_germana = sum(parsed[8:10])
                    
                    # Si es un diccionario (formato nuevo)
                    elif isinstance(parsed, dict):
                        sus_total = parsed.get('sus_total')
                        carga_intrinseca = parsed.get('carga_intrinseca')
                        carga_extrinseca = parsed.get('carga_extrinseca')
                        carga_germana = parsed.get('carga_germana')
                        
                except Exception as e:
                    print(f"Error parseando JSON: {e}")
            
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
        print(f"✅ Total encuestas: {len(encuestas)}")
        return jsonify(encuestas)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ Error en get_encuestas: {e}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/admin/usuarios/<int:usuario_id>', methods=['DELETE'])
@admin_required
def eliminar_usuario(usuario_id):
    try:
        usuario = Usuario.query.get(usuario_id)
        if not usuario:
            return jsonify({'error': 'Usuario no encontrado'}), 404
        
        # Eliminar reflexiones de las sesiones del usuario
        for sesion in usuario.sesiones:
            ReflexionMetacognitiva.query.filter_by(sesion_id=sesion.id).delete()
            ResultadoEncuesta.query.filter_by(sesion_id=sesion.id).delete()
            ResultadoIdentificacion.query.filter_by(sesion_id=sesion.id).delete()
                # Eliminar las sesiones
        SesionExperimental.query.filter_by(usuario_id=usuario_id).delete()
        
        # Eliminar el usuario
        db.session.delete(usuario)
        db.session.commit()
        
        return jsonify({'mensaje': 'Usuario eliminado correctamente'}), 200
    except Exception as e:
        db.session.rollback()
        print(f"Error eliminando usuario: {e}")
        return jsonify({'error': str(e)}), 500        
# ===== API EXPERIMENTO =====
@app.route('/api/experimento/iniciar', methods=['POST'])
def iniciar_experimento():
    try:
        data = request.json
        usuario = Usuario.query.get(data['usuario_id'])
        if not usuario:
            return jsonify({'error': 'Usuario no encontrado'}), 404
        
        # Obtener pool de especímenes basado en especies activas
        pool = get_pool_especimenes()
        
        # Seleccionar 2 especímenes aleatorios
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
        print(f"❌ Error iniciando sesión: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/experimento/guardar_resultado', methods=['POST'])
def guardar_resultado():
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
        return jsonify({'error': str(e)}), 500

@app.route('/api/experimento/guardar_reflexion', methods=['POST'])
def guardar_reflexion():
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
        return jsonify({'error': str(e)}), 500

@app.route('/api/experimento/guardar_encuesta', methods=['POST'])
def guardar_encuesta():
    try:
        data = request.json
        respuestas = data['respuestas']
        
        # Calcular SUS si es el caso y no viene puntaje
        puntaje_total = data.get('puntaje_total')
        if data['tipo'] == 'SUS' and not puntaje_total:
            # Cálculo básico SUS (formato estándar: preguntas 1-10)
            if isinstance(respuestas, dict):
                valores = [int(v) for v in respuestas.values() if str(v).isdigit()]
                if len(valores) == 10:
                    # Fórmula SUS: (suma de impares - 5) + (25 - suma de pares) * 2.5
                    suma_impares = sum(valores[i] for i in range(0, 10, 2))
                    suma_pares = sum(valores[i] for i in range(1, 10, 2))
                    puntaje_total = ((suma_impares - 5) + (25 - suma_pares)) * 2.5
                    puntaje_total = max(0, min(100, puntaje_total))
        
        encuesta = ResultadoEncuesta(
            sesion_id=data['sesion_id'],
            tipo=data['tipo'],
            respuestas_json=json.dumps(respuestas),
            puntaje_total=puntaje_total
        )
        db.session.add(encuesta)
        db.session.commit()
        return jsonify({'mensaje': 'Encuesta guardada'})
    except Exception as e:
        print(f"❌ Error guardando encuesta: {e}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/usuario/<int:usuario_id>/encuestas/completadas', methods=['GET'])
def encuestas_completadas(usuario_id):
    try:
        # Obtener la última sesión del usuario
        sesion = SesionExperimental.query.filter_by(usuario_id=usuario_id).order_by(SesionExperimental.id.desc()).first()
        
        if not sesion:
            return jsonify({
                'sus_completada': False,
                'carga_completada': False,
                'ambas_completadas': False,
                'sesion_id': None
            })
        
        # Verificar encuestas de esa sesión
        encuestas = ResultadoEncuesta.query.filter_by(sesion_id=sesion.id).all()
        sus_completada = any(e.tipo == 'SUS' for e in encuestas)
        carga_completada = any(e.tipo == 'COGNITIVE_LOAD' for e in encuestas)
        
        return jsonify({
            'sus_completada': sus_completada,
            'carga_completada': carga_completada,
            'ambas_completadas': sus_completada and carga_completada,
            'sesion_id': sesion.id
        })
    except Exception as e:
        print(f"Error en encuestas_completadas: {e}")
        return jsonify({'error': str(e)}), 500      
@app.route('/api/admin/reflexiones', methods=['GET'])
@admin_required
def get_reflexiones():
    """Devuelve todas las reflexiones metacognitivas para análisis"""
    try:
        reflexiones = []
        for ref in ReflexionMetacognitiva.query.all():
            # Obtener información de la sesión y usuario
            sesion = SesionExperimental.query.get(ref.sesion_id)
            if not sesion:
                continue
            
            usuario = Usuario.query.get(sesion.usuario_id)
            if not usuario:
                continue
            
            # Parsear la respuesta (formato: "Clave: valor | Clave2: valor2")
            respuesta = ref.respuesta or ""
            
            # Extraer valores numéricos según el momento
            datos = {
                'id': ref.id,
                'usuario': usuario.nombre_usuario,
                'grupo': sesion.grupo,
                'momento': ref.momento,
                'pregunta': ref.pregunta,
                'respuesta_raw': respuesta,
                'timestamp': ref.timestamp.isoformat() if ref.timestamp else None
            }
            
            # Extraer valores específicos según el tipo de reflexión
            if 'conocimiento' in respuesta.lower() or 'Confianza' in respuesta or 'seguro' in respuesta:
                import re
                numeros = re.findall(r'(\d+)%', respuesta)
                if len(numeros) >= 1:
                    datos['valor1'] = int(numeros[0])
                if len(numeros) >= 2:
                    datos['valor2'] = int(numeros[1])
                if len(numeros) >= 3:
                    datos['valor3'] = int(numeros[2])
            
            reflexiones.append(datos)
        
        return jsonify(reflexiones)
    except Exception as e:
        print(f"❌ Error en get_reflexiones: {e}")
        return jsonify({'error': str(e)}), 500    
# ===== INICIALIZACIÓN =====
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Configurar especies activas por defecto (las primeras 4)
        set_especies_activas([e['id'] for e in POOL_ESPECIES[:4]])
        print("✅ Base de datos lista")
        print("🚀 Servidor en http://127.0.0.1:5000")
        print("🔐 Admin login: admin / admin123")
    app.run(debug=False, port=5000)