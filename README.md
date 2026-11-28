# SOLTAT (Sistema de Optimización Logística y Turnos de Acopio Tabacalero)
Sistema web de gestión de turnos y cupos para el centro de acopio tabacalero de Perico (Jujuy), compartido por la Cooperativa de Tabacaleros de Jujuy, Alliance One y Massalin Particulares.
**Ingeniería & Desarrollo:** 3B SYSTEMS

---

## 🚀 Puesta en marcha rápida

El proyecto incluye la base de datos `db.sqlite3` con datos de prueba y usuarios demo listos para usar.

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

Abrir en el navegador: **`http://127.0.0.1:8000/`**

---

## 🔑 Usuarios de prueba

Se ingresa con el DNI/CUIT (o el nombre de usuario) y la contraseña.

| Tipo de usuario | DNI / CUIT | Contraseña | Qué puede hacer |
|---|---|---|---|
| Productor (nivel bueno) | `20345678901` | `productor123` | Solicitar y cancelar turnos, ver comprobantes, historial y rendimiento |
| Productor (nivel regular) | `20287654321` | `productor123` | Ídem (sin restricción horaria) |
| Productor (bloqueado) | `20312233445` | `productor123` | No puede solicitar turnos hasta que venza el bloqueo |
| Recorredor (Cooperativa) | `20111111111` | `recorredor123` | Confirmar o rechazar solicitudes, asignar turnos |
| Operador de acopio | `20222222222` | `operador123` | Registrar ingreso en garita, pesaje y descarga (todas las empresas) |
| Empresa (Cooperativa) | `30554433221` | `empresa123` | Ver ocupación diaria y por hora, línea de tiempo de sus rampas, gráficos y convocatorias |
| Administrador | `20000000001` | `admin123` | Gestionar usuarios, empresas y cupos en `/admin` |

Hay también recorredores de Alliance One (`20111111112`) y Massalin (`20111111113`), y las empresas `30665544332` y `30776655443`, con las mismas contraseñas de su tipo.

---

## 🧩 Modelo (según el diagrama de clases)

- **Usuario** (`cuit_dni`, `telefono`) y sus subclases: **Productor**, **Recorredor** (con `empresa_asignada`), **OperadorAcopio**, **Administrador** y **EmpresaAcopio** (`nombre`, `capacidad_diaria_kg`, `velocidad_descarga_kg_hora`, `horario_preferente`, `activa`, `corte_solicitado`; `capacidad_franja_kg` queda solo como dato de referencia).
- **CupoProductor**: kilos totales que un productor puede entregar a una empresa en la temporada.
- **Turno**: un solo **corte de tabaco** por turno (BAJERA, MEDIANA, SUPERIOR) y estado **PENDIENTE → CONFIRMADO → EN_PLAYA → DESCARGADO** (o CANCELADO). Guarda `hora_inicio`, `hora_fin`, `duracion_min` y `rampa`; los turnos anteriores a la duración variable solo tienen `franja_horaria` y se muestran como su franja de 2 h.
- **Rampa** (1 a 5, con sus empresas habilitadas) y **OcupacionRampa**: los tramos de 30 min que reserva cada turno en su rampa.
- **RegistroOperador**: pesaje en báscula (peso acordado, peso real, corte).
- **Comprobante**: se emite en la báscula, al registrar el pesaje, con el peso real, el corte y el precio por kg.

## ⚙️ Reglas principales

- **Turnos:** el productor los solicita y quedan PENDIENTES hasta que el recorredor de la empresa los confirma. Los que asigna el recorredor nacen CONFIRMADOS.
- **Duración de cada turno:** depende de los kilos. Duración = kilos ÷ velocidad de descarga de la empresa (Cooperativa 20.000, Alliance One 17.500 y Massalin 15.000 kg/h; se cambia en `/admin`), redondeada hacia arriba a tramos de 30 minutos. Ej.: 100.000 kg a 17.500 kg/h = 6 h. No hay un máximo de kilos por turno: el límite es la capacidad diaria de la empresa y el horario de 06:00 a 22:00.
- **Asignación:** el sistema busca el primer bloque continuo (con inicios cada 30 min) en el que una rampa de la empresa esté libre durante todo el turno, empezando por el horario preferente de la empresa. Si ese día no hay lugar, sugiere u ofrece el próximo horario disponible.
- **Rampas de descarga:** 5 rampas, un camión por rampa a la vez. Massalin usa la 1 y la 2, Alliance One de la 3 a la 5 (prioridad de 06:00 a 14:00) y la Cooperativa las 5 (prioridad de 14:00 a 22:00). La prioridad se respeta en cada tramo del turno; fuera de su horario una empresa solo toma tramos libres el mismo día de la entrega. Cada tramo reservado se guarda en `OcupacionRampa`, cuya restricción única impide superposiciones aun con reservas simultáneas.
- **Premios y castigos** (últimos 90 días). Cuentan como incumplimiento: no presentarse, cancelar un turno confirmado y un desvío de kilos mayor al 20 %.
  - 0 incumplimientos: nivel **Bueno**, sin restricciones.
  - 1: **Regular**, sin restricción horaria.
  - 2: **Malo**, el turno se asigna al menos 5 días después y desde el último día con turnos de la empresa; ese día empieza **después del último turno de la agenda** (nunca antes ni al mismo tiempo), respetando rampas, prioridades, capacidad diaria y horario.
  - 3 o más: **Bloqueado** por 30 días desde el último; solo el recorredor puede asignarle turnos.
- **Avisos por WhatsApp:** el sistema arma el mensaje y un enlace `wa.me` con el número y el texto cargados; el usuario solo toca Enviar. Se usa en confirmaciones, cancelaciones y convocatorias.

Los parámetros (90 días, 20 %, 30 días, demora del nivel Malo, liberación de rampas) están al principio de `turnos/services.py`; el tramo de 30 min y el horario de 06:00 a 22:00, en `turnos/choices.py`.

### API `/api/disponibilidad/?empresa_id=…&fecha=AAAA-MM-DD`

Devuelve la ocupación del día. Claves nuevas: `velocidad_descarga` y `horas` (por cada hora: `ocupados` y `capacidad` según las rampas disponibles). Las claves anteriores `capacidad_franja` y `franjas` se mantienen por compatibilidad pero están **obsoletas**: `franjas` se calcula sumando las horas de cada franja de 2 h.

---

## 🗄️ Base de datos

- **SQLite (por defecto):** ya viene cargada en `db.sqlite3`. Para volver a cargar los datos de prueba:
  ```bash
  python manage.py migrate
  python manage.py seed_data --reset
  ```
- **Después de actualizar la rama:** correr siempre `python manage.py migrate`. Las migraciones de duración variable (`accounts 0003`, `turnos 0008` a `0010`) no modifican los turnos existentes: agregan columnas vacías, calculan la velocidad de cada empresa (capacidad por franja / 2) y registran en `OcupacionRampa` las rampas que ya ocupaban los turnos activos. **La migración `turnos 0008` no se puede revertir** una vez que hay turnos de duración variable (la restricción anterior de un turno por franja no admite turnos que empiezan cada 30 min).
- **Concurrencia:** SQLite está configurado con `transaction_mode: IMMEDIATE` y `timeout: 20` (`core/settings.py`), así las reservas simultáneas se atienden de a una. En PostgreSQL las reservas bloquean la empresa y las rampas (`select_for_update`), y en ambos motores la restricción única de `OcupacionRampa` impide superposiciones.
- **PostgreSQL 18:** poner `DB_ENGINE=postgres` en el archivo `.env`, ejecutar `migrate` y `seed_data`. El script `sql/pgadmin4_soltat.sql` documenta el esquema e incluye consultas para pgAdmin 4 (ocupación, planilla de garita, cumplimiento y cupos).

## ✅ Tests
```bash
python manage.py test turnos
```
