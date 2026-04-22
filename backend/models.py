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
    rol = db.Column(db.String(20), default='usuario')  # 'admin' o 'usuario' ← NUEVO
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