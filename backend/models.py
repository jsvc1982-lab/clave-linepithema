from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
db = SQLAlchemy()
class Usuario(db.Model):
    __tablename__ = 'usuarios'
    
    id = db.Column(db.Integer, primary_key=True)
    nombre_usuario = db.Column(db.String(50), unique=True, nullable=False)
    correo = db.Column(db.String(100), unique=True, nullable=False)
    contrasena_hash = db.Column(db.String(200), nullable=False)
    semestre = db.Column(db.Integer)
    curso = db.Column(db.Integer)
    genero = db.Column(db.String(20), default='No especificado')
    experiencia_taxonomica = db.Column(db.Integer, default=3)
    habilidad_espacial = db.Column(db.Integer, default=12)
    familiaridad_3d = db.Column(db.Integer, default=3)
    grupo_asignado = db.Column(db.String(20))  # '2D', '2D_META', '3D', '3D_META'
    rol = db.Column(db.String(20), default='usuario')  # 'admin' o 'usuario'
    fecha_registro = db.Column(db.DateTime, default=datetime.utcnow)
    
    sesiones = db.relationship('SesionExperimental', backref='usuario_rel', lazy=True)
class SesionExperimental(db.Model):
    __tablename__ = 'sesiones'
    
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    fecha_inicio = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_fin = db.Column(db.DateTime)
    grupo = db.Column(db.String(20))
    especies_asignadas = db.Column(db.Text)  # JSON con las 2 especies que le tocaron
    especimenes_asignados = db.Column(db.Text)  # Para compatibilidad
    
    resultados = db.relationship('ResultadoIdentificacion', backref='sesion_rel', lazy=True)
    encuestas = db.relationship('ResultadoEncuesta', backref='sesion_rel', lazy=True)
    reflexiones = db.relationship('ReflexionMetacognitiva', backref='sesion_rel', lazy=True)
    pasos = db.relationship('PasoClave', backref='sesion_rel', lazy=True)
class ResultadoIdentificacion(db.Model):
    __tablename__ = 'resultados'
    
    id = db.Column(db.Integer, primary_key=True)
    sesion_id = db.Column(db.Integer, db.ForeignKey('sesiones.id'), nullable=False)
    especie_id = db.Column(db.String(50))
    especie_correcta = db.Column(db.String(50))
    especie_seleccionada = db.Column(db.String(50))
    es_correcta = db.Column(db.Boolean)
    tiempo_segundos = db.Column(db.Float)
    orden = db.Column(db.Integer)
class ResultadoEncuesta(db.Model):
    __tablename__ = 'encuestas'
    
    id = db.Column(db.Integer, primary_key=True)
    sesion_id = db.Column(db.Integer, db.ForeignKey('sesiones.id'), nullable=False)
    tipo = db.Column(db.String(20))  # 'SUS', 'COGNITIVE_LOAD'
    respuestas_json = db.Column(db.Text)
    puntaje_total = db.Column(db.Float)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
class ReflexionMetacognitiva(db.Model):
    __tablename__ = 'reflexiones'
    
    id = db.Column(db.Integer, primary_key=True)
    sesion_id = db.Column(db.Integer, db.ForeignKey('sesiones.id'), nullable=False)
    momento = db.Column(db.String(20))  # 'pre', 'durante', 'post'
    pregunta = db.Column(db.Text)
    respuesta = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
class Configuracion(db.Model):
    __tablename__ = 'configuracion'
    
    id = db.Column(db.Integer, primary_key=True)
    clave = db.Column(db.String(50), unique=True, nullable=False)
    valor = db.Column(db.Text)
    actualizado = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PasoClave(db.Model):
    """Registra cada clic del estudiante al navegar la clave dicotómica:
    en qué pregunta estaba, qué opción eligió, cuánto tiempo tardó en
    decidir, y si esa elección coincidía con el camino correcto hacia
    la especie real del espécimen. Permite reconstruir el recorrido
    completo por espécimen para el dashboard del estudiante y del admin."""
    __tablename__ = 'pasos_clave'

    id = db.Column(db.Integer, primary_key=True)
    sesion_id = db.Column(db.Integer, db.ForeignKey('sesiones.id'), nullable=False)
    especimen_id = db.Column(db.String(50))
    especimen_orden = db.Column(db.Integer)      # 1 o 2 (cuál de los 2 especímenes de la sesión)
    pregunta = db.Column(db.Integer)             # número de pregunta de la clave (1-8)
    opcion_elegida = db.Column(db.Integer)       # 0 = opción A, 1 = opción B
    es_paso_correcto = db.Column(db.Boolean)     # si esta elección coincide con el camino correcto
    es_paso_final = db.Column(db.Boolean, default=False)  # si este paso concluyó en una especie
    tiempo_segundos = db.Column(db.Float)        # tiempo empleado en decidir este paso
    orden_paso = db.Column(db.Integer)           # 1er clic, 2do clic... dentro del espécimen
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
