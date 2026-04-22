import sys
import os

# Agregar la carpeta backend al path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

# Cambiar al directorio backend para que funcione
os.chdir(os.path.join(os.path.dirname(__file__), 'backend'))

from app import app, db

with app.app_context():
    print("Creando tablas...")
    db.create_all()
    print("✅ Tablas creadas exitosamente")
    
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    tablas = inspector.get_table_names()
    print(f"Tablas creadas: {tablas}")