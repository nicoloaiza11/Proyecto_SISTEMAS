# SOLTAT (Sistema de Optimización Logística y Turnos de Acopio Tabacalero)
Desarrollado para la optimización logística, control estricto de cupos de acopio y sistema de apoyo a la toma de decisiones (SAD) gerencial y de finca.  
**Ingeniería & Desarrollo:** 3B SYSTEMS

---

## 🚀 Puesta en marcha rápida (Para cualquier compañero de equipo)

El proyecto ya incluye la base de datos funcional preconfigurada (`db.sqlite3`) con el histórico de 191 movimientos y usuarios demo listos para usar.

### 1. Clonar el repositorio
```bash
git clone https://github.com/nicoloaiza11/Proyecto_SISTEMAS.git
cd Proyecto_SISTEMAS
```

### 2. Crear y activar el entorno virtual
En Windows (PowerShell):
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```
En Linux / macOS:
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Instalar las dependencias
```bash
pip install -r requirements.txt
```

### 4. Ejecutar el servidor local
```bash
python manage.py runserver
```

Abre en tu navegador: **`http://127.0.0.1:8000/`**

---

## 🔑 Accesos de prueba disponibles

- **Portal del Productor:**
  - CUIT / DNI: `20345678901`
  - Contraseña: `productor123`
  - *Funciones:* Reserva de cupo, notificación de salida de finca y tablero de rendimiento agropecuario.

- **Portal de Planta de Acopio (Cooperativa Jujuy):**
  - CUIT / DNI: `30554433221`
  - Contraseña: `empresa123`
  - *Funciones:* Planilla en vivo de playa/báscula, control de capacidad diaria (250.000 kg) e Inteligencia Gerencial SAD.

---

## 🗄️ Base de datos y arquitectura
- **SQLite (por defecto para ejecución inmediata):** Ya viene poblada en `db.sqlite3`. Si deseas resembrar los datos desde cero:
  ```bash
  python manage.py migrate
  python manage.py seed_data
  ```
- **PostgreSQL 18:** El repositorio incluye el script DDL y de auditoría gerencial en `sql/pgadmin4_soltat.sql` listo para ejecutar en **pgAdmin 4**.
