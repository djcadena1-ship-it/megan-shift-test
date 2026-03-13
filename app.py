import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime, timedelta
import re
import os
import tempfile
import math

try:
    from fpdf import FPDF
    HAS_FPDF = True
except ImportError:
    HAS_FPDF = False

# Configuración estilo ERP
st.set_page_config(page_title="Megan Shift Planning JC", layout="wide", initial_sidebar_state="expanded")

# --- DICCIONARIOS MAESTROS ---
ecuador = {
    "Azuay": ["Cuenca", "Gualaceo", "Paute"],
    "Guayas": ["Guayaquil", "Durán", "Samborondón", "Daule", "Milagro"],
    "Manabí": ["Manta", "Portoviejo", "Chone", "Montecristi"],
    "Pichincha": ["Quito", "Cayambe", "Machachi", "Sangolquí"],
    "Los Ríos": ["Quevedo", "Babahoyo", "Buena Fe"],
    "El Oro": ["Machala", "Pasaje", "Santa Rosa"]
}

diccionario_novedades = {
    "1 - FALTA": "1",
    "2 - ATRASO": "2",
    "3 - PERMISO": "3",
    "4 - ENFERMEDAD": "4",
    "5 - FIN DE SERVICIO": "5",
    "6 - BAJA": "6",
    "7 - CAMBIO DE HORARIO": "7",
    "8 - SERVICIO ADICIONAL": "8",
    "9 - RELEVO ALMUERZO": "9",
    "10 - FERIADO": "10",
    "11 - CAMBIO OPERATIVO": "11",
    "12 - CITA MEDICA": "12",
    "13 - SEMANA INTEGRAL": "13"
}

# ==========================================
# 1. BASE DE DATOS Y PARCHES
# ==========================================
conn = sqlite3.connect("data/sistema_seguridad.db", check_same_thread=False, timeout=15)
c = conn.cursor()

c.execute('CREATE TABLE IF NOT EXISTS clientes (id INTEGER PRIMARY KEY, codigo TEXT UNIQUE, nombre TEXT)')
c.execute('CREATE TABLE IF NOT EXISTS puestos (id INTEGER PRIMARY KEY, cliente_id INTEGER, nombre TEXT, provincia TEXT, ciudad TEXT)')
c.execute('CREATE TABLE IF NOT EXISTS guardias (id INTEGER PRIMARY KEY, puesto_id INTEGER, cedula TEXT, nombres TEXT, codigo_horario TEXT)')
c.execute('''CREATE TABLE IF NOT EXISTS codigos_horario 
             (nombre_codigo TEXT, dia_numero INTEGER, dia_nombre TEXT, ingreso TEXT, salida TEXT, hrs TEXT, rn TEXT, extra_50 TEXT, extra_100 TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS novedades 
             (id INTEGER PRIMARY KEY, guardia_ausente_id INTEGER, guardia_reemplazo_id INTEGER, fecha TEXT, tipo TEXT, motivo TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS programacion_diaria (
             id INTEGER PRIMARY KEY,
             guardia_id INTEGER,
             fecha TEXT,
             ingreso TEXT,
             salida TEXT,
             hrs REAL,
             rn REAL,
             extra_50 REAL,
             extra_100 REAL
             )''')
c.execute('''CREATE TABLE IF NOT EXISTS periodos (
             id INTEGER PRIMARY KEY,
             nombre TEXT UNIQUE,
             f_inicio TEXT,
             f_fin TEXT,
             estado TEXT DEFAULT 'Pendiente'
             )''')

# --- NUEVAS TABLAS MÓDULO RRHH ---
c.execute('CREATE TABLE IF NOT EXISTS cargos (id INTEGER PRIMARY KEY, nombre TEXT UNIQUE)')
c.execute('''CREATE TABLE IF NOT EXISTS empleados (
             nui TEXT PRIMARY KEY, 
             apellidos TEXT, 
             nombres TEXT, 
             cargo TEXT, 
             centro_costo TEXT, 
             coordinador TEXT, 
             provincia TEXT, 
             ciudad TEXT, 
             f_salida TEXT, 
             estado TEXT DEFAULT 'Activo'
             )''')
c.execute('''CREATE TABLE IF NOT EXISTS historico_sueldos (
             id INTEGER PRIMARY KEY, 
             empleado_nui TEXT, 
             sueldo REAL, 
             f_inicio TEXT, 
             f_fin TEXT
             )''')

try: c.execute("ALTER TABLE programacion_diaria ADD COLUMN operador TEXT DEFAULT ''")
except: pass
try: c.execute("ALTER TABLE programacion_diaria ADD COLUMN novedad TEXT DEFAULT '0'")
except: pass
try: c.execute("ALTER TABLE programacion_diaria ADD COLUMN puesto_id INTEGER DEFAULT 0")
except: pass
try: c.execute("ALTER TABLE novedades ADD COLUMN motivo TEXT DEFAULT ''")
except: pass
try: c.execute("ALTER TABLE puestos ADD COLUMN horas_semana REAL DEFAULT 84")
except: pass
try: c.execute("ALTER TABLE guardias ADD COLUMN periodo_id INTEGER DEFAULT 0")
except: pass
try: c.execute("ALTER TABLE puestos ADD COLUMN secuencia INTEGER DEFAULT 0")
except: pass
try: c.execute("ALTER TABLE puestos ADD COLUMN estado TEXT DEFAULT 'Habilitado'")
except: pass
try: c.execute("ALTER TABLE codigos_horario ADD COLUMN fecha_base TEXT DEFAULT 'SEMANAL'")
except: pass

try: c.execute("UPDATE guardias SET periodo_id = (SELECT MIN(id) FROM periodos) WHERE periodo_id = 0 OR periodo_id IS NULL")
except sqlite3.OperationalError: pass

c.execute("SELECT id, cliente_id FROM puestos WHERE secuencia = 0 OR secuencia IS NULL")
puestos_sin_sec = c.fetchall()
for p_id, c_id in puestos_sin_sec:
    c.execute("SELECT MAX(secuencia) FROM puestos WHERE cliente_id=?", (c_id,))
    max_val = c.fetchone()[0]
    next_val = (max_val or 0) + 1
    c.execute("UPDATE puestos SET secuencia=? WHERE id=?", (next_val, p_id))

conn.commit()

# ==========================================
# 2. FUNCIONES DEL MOTOR MATEMÁTICO Y PDF
# ==========================================
def str_to_float(val):
    if str(val).upper() in ['D', '0', ''] or val is None: return 0.0
    try: return float(val)
    except: return 0.0

def formatear_hora_input(hora_str):
    hora_str = str(hora_str).strip().upper()
    if hora_str in ['0', 'D', '']: return '0'
    if hora_str in ["2400", "24:00"]: return "00:00"
    if ':' in hora_str:
        try:
            datetime.strptime(hora_str, "%H:%M")
            return hora_str
        except ValueError: return '0'
    hora_str = re.sub(r'\D', '', hora_str) 
    if len(hora_str) == 3: hora_str = '0' + hora_str
    if len(hora_str) == 4:
        try:
            hora_formateada = f"{hora_str[:2]}:{hora_str[2:]}"
            datetime.strptime(hora_formateada, "%H:%M")
            return hora_formateada
        except ValueError: return '0'
    return '0'

def calcular_horas(hora_entrada, hora_salida):
    if hora_entrada == "0" or hora_salida == "0": return "0"
    if hora_entrada == "D" or hora_salida == "D": return "0"
    try:
        e = datetime.strptime(hora_entrada, "%H:%M")
        s = datetime.strptime(hora_salida, "%H:%M")
        if s < e: s += timedelta(days=1)
        diff = (s - e).seconds / 3600.0
        return f"{diff:g}" 
    except: return "0"

def obtener_plantilla_codigo(nombre_codigo):
    return pd.read_sql_query(f"SELECT * FROM codigos_horario WHERE nombre_codigo = '{nombre_codigo}' ORDER BY dia_numero", conn)

def formato_hora_csv(hora_str):
    if hora_str in ["D", "0", "FALTA", "REEMPLAZO", "ANULADO"] or not hora_str: return "0"
    limpio = str(hora_str).replace(":", "").lstrip("0")
    return limpio if limpio else "0"

def hay_cruce_horarios(h_ini1, h_fin1, h_ini2, h_fin2):
    if h_ini1 in ["0", "D", ""] or h_fin1 in ["0", "D", ""]: return False
    if h_ini2 in ["0", "D", ""] or h_fin2 in ["0", "D", ""]: return False
    
    try:
        base_date = datetime(2000, 1, 1)
        t1_ini = datetime.strptime(h_ini1, "%H:%M")
        t1_fin = datetime.strptime(h_fin1, "%H:%M")
        dt1_ini = base_date.replace(hour=t1_ini.hour, minute=t1_ini.minute)
        dt1_fin = base_date.replace(hour=t1_fin.hour, minute=t1_fin.minute)
        if dt1_fin <= dt1_ini: dt1_fin += timedelta(days=1)
        
        t2_ini = datetime.strptime(h_ini2, "%H:%M")
        t2_fin = datetime.strptime(h_fin2, "%H:%M")
        dt2_ini = base_date.replace(hour=t2_ini.hour, minute=t2_ini.minute)
        dt2_fin = base_date.replace(hour=t2_fin.hour, minute=t2_fin.minute)
        if dt2_fin <= dt2_ini: dt2_fin += timedelta(days=1)
        
        return (dt1_ini < dt2_fin) and (dt2_ini < dt1_fin)
    except Exception as e:
        return False

def sanitize_for_fpdf(text):
    if text is None: return ""
    text = str(text)
    text = text.replace('\u2013', '-').replace('\u2014', '-')
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    return text.encode('latin-1', 'replace').decode('latin-1')

def generar_pdf_horarios(df_pdf, nombre_periodo):
    pdf = FPDF(orientation='L', unit='mm', format='A4')
    empleados = df_pdf['nombres'].unique()
    
    for emp in empleados:
        pdf.add_page()
        
        pdf.set_fill_color(41, 128, 185) 
        pdf.set_text_color(255, 255, 255)
        pdf.set_font('Arial', 'B', 16)
        pdf.cell(0, 12, sanitize_for_fpdf('Megan Shift Planning - Hoja de Programación Base'), 0, 1, 'C', fill=True)
        pdf.ln(5)
        
        df_emp = df_pdf[df_pdf['nombres'] == emp].reset_index(drop=True)
        cedula = df_emp['cedula'].iloc[0]
        empresa = df_emp['empresa'].iloc[0]
        puesto = df_emp['puesto'].iloc[0]
        
        pdf.set_text_color(0, 0, 0)
        pdf.set_font('Arial', 'B', 10)
        
        pdf.cell(25, 6, 'Empleado:', 0, 0)
        pdf.set_font('Arial', '', 10)
        pdf.cell(100, 6, sanitize_for_fpdf(f"{emp} (CI: {cedula})"), 0, 0)
        
        pdf.set_font('Arial', 'B', 10)
        pdf.cell(25, 6, 'Periodo:', 0, 0)
        pdf.set_font('Arial', '', 10)
        pdf.cell(0, 6, sanitize_for_fpdf(nombre_periodo), 0, 1)
        
        pdf.set_font('Arial', 'B', 10)
        pdf.cell(25, 6, 'Empresa:', 0, 0)
        pdf.set_font('Arial', '', 10)
        pdf.cell(100, 6, sanitize_for_fpdf(empresa), 0, 0)
        
        pdf.set_font('Arial', 'B', 10)
        pdf.cell(25, 6, 'Puesto:', 0, 0)
        pdf.set_font('Arial', '', 10)
        pdf.cell(0, 6, sanitize_for_fpdf(puesto), 0, 1)
        
        pdf.ln(8)
        
        total_rows = len(df_emp)
        mid = math.ceil(total_rows / 2) 
        
        df_left = df_emp.iloc[:mid].reset_index(drop=True)
        df_right = df_emp.iloc[mid:].reset_index(drop=True)
        
        x_izq = 36
        x_der = 156
        col_w = [30, 25, 25, 25] 
        h_row = 6 
        
        pdf.set_font('Arial', 'B', 10)
        pdf.set_fill_color(200, 220, 240) 
        
        pdf.set_x(x_izq)
        pdf.cell(col_w[0], 8, 'Fecha', 1, 0, 'C', fill=True)
        pdf.cell(col_w[1], 8, 'Inicio', 1, 0, 'C', fill=True)
        pdf.cell(col_w[2], 8, 'Fin', 1, 0, 'C', fill=True)
        pdf.cell(col_w[3], 8, 'Horas', 1, 0, 'C', fill=True)
        
        pdf.set_x(x_der)
        pdf.cell(col_w[0], 8, 'Fecha', 1, 0, 'C', fill=True)
        pdf.cell(col_w[1], 8, 'Inicio', 1, 0, 'C', fill=True)
        pdf.cell(col_w[2], 8, 'Fin', 1, 0, 'C', fill=True)
        pdf.cell(col_w[3], 8, 'Horas', 1, 1, 'C', fill=True)
        
        pdf.set_font('Arial', '', 10)
        y_start_tables = pdf.get_y() 
        
        for i in range(mid):
            fill_color = bool(i % 2 != 0)
            if fill_color:
                pdf.set_fill_color(245, 245, 245)
            else:
                pdf.set_fill_color(255, 255, 255)
                
            pdf.set_xy(x_izq, y_start_tables + i * h_row)
            if i < len(df_left):
                row_l = df_left.iloc[i]
                pdf.cell(col_w[0], h_row, sanitize_for_fpdf(row_l['fecha']), 1, 0, 'C', fill=True)
                pdf.cell(col_w[1], h_row, sanitize_for_fpdf(row_l['ingreso']), 1, 0, 'C', fill=True)
                pdf.cell(col_w[2], h_row, sanitize_for_fpdf(row_l['salida']), 1, 0, 'C', fill=True)
                pdf.cell(col_w[3], h_row, sanitize_for_fpdf(str(row_l['hrs'])), 1, 0, 'C', fill=True)
                
            pdf.set_xy(x_der, y_start_tables + i * h_row)
            if i < len(df_right):
                row_r = df_right.iloc[i]
                pdf.cell(col_w[0], h_row, sanitize_for_fpdf(row_r['fecha']), 1, 0, 'C', fill=True)
                pdf.cell(col_w[1], h_row, sanitize_for_fpdf(row_r['ingreso']), 1, 0, 'C', fill=True)
                pdf.cell(col_w[2], h_row, sanitize_for_fpdf(row_r['salida']), 1, 0, 'C', fill=True)
                pdf.cell(col_w[3], h_row, sanitize_for_fpdf(str(row_r['hrs'])), 1, 0, 'C', fill=True)
        
        pdf.set_y(y_start_tables + mid * h_row + 25)
            
        y_firma = pdf.get_y()
        pdf.line(50, y_firma, 120, y_firma)
        pdf.line(177, y_firma, 247, y_firma)
        
        pdf.set_xy(50, y_firma + 2)
        pdf.set_font('Arial', 'B', 10)
        pdf.cell(70, 5, 'Firma del Empleado', 0, 0, 'C')
        
        pdf.set_xy(177, y_firma + 2)
        pdf.cell(70, 5, 'Firma del Coordinador', 0, 1, 'C')
        
        pdf.set_xy(177, y_firma + 8)
        pdf.set_font('Arial', '', 10)
        pdf.cell(70, 5, 'Nombre: ____________________', 0, 1, 'C')
        
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        pdf.output(tmp.name)
        with open(tmp.name, "rb") as f:
            pdf_bytes = f.read()
    os.remove(tmp.name)
    return pdf_bytes

# ==========================================
# 3. INTERFAZ SAP (SIDEBAR Y MÓDULOS)
# ==========================================
st.sidebar.title("Megan Shift Planning JC")

menu = st.sidebar.radio(
    "Navegación de Módulos",
    [
        "🏠 Dashboard de Operaciones", 
        "👥 Maestro de Personal (RRHH)", # NUEVO MÓDULO
        "📁 Maestro de clientes", 
        "🌍 Periodos y generacion de la programacion", 
        "🔄 Transacciones de la programacion", 
        "📥 Reportes"
    ]
)

st.sidebar.divider()
st.sidebar.info("Usuario: ADMIN\n\nRol: HR Manager\n\nMandante: 100")

# ------------------------------------------
# MÓDULO 0: DASHBOARD
# ------------------------------------------
if menu == "🏠 Dashboard de Operaciones":
    st.title("Sistema de Gestión de Recursos Humanos y Operaciones")
    
    total_clientes = pd.read_sql_query("SELECT COUNT(*) FROM clientes", conn).iloc[0,0]
    total_puestos = pd.read_sql_query("SELECT COUNT(*) FROM puestos", conn).iloc[0,0]
    total_empleados = pd.read_sql_query("SELECT COUNT(DISTINCT nui) FROM empleados WHERE estado='Activo'", conn).iloc[0,0]
    
    c1, c2, c3 = st.columns(3)
    c1.metric("Empresas / Clientes", total_clientes)
    c2.metric("Centros de Costo (Puestos)", total_puestos)
    c3.metric("Maestro de Personal (Activos)", total_empleados)
    
    st.divider()
    st.subheader("📊 Control Operativo y Financiero (Variaciones de Nómina)")
    
    df_periodos = pd.read_sql_query("SELECT * FROM periodos", conn)
    
    if not df_periodos.empty:
        dicc_per = dict(zip(df_periodos['nombre'], df_periodos['id']))
        sel_periodo_nom = st.selectbox("Seleccione el Periodo a Analizar:", list(dicc_per.keys()))
        per_info = df_periodos[df_periodos['nombre'] == sel_periodo_nom].iloc[0]
        f_ini = per_info['f_inicio']
        f_fin = per_info['f_fin']
        
        query_metrics = f'''
            SELECT 
                SUM(CASE WHEN operador = '' THEN hrs ELSE 0 END) as prog_hrs,
                SUM(CASE WHEN operador IN ('', '+') THEN hrs WHEN operador = '-' THEN -hrs ELSE 0 END) as real_hrs,
                
                SUM(CASE WHEN operador = '' THEN rn ELSE 0 END) as prog_rn,
                SUM(CASE WHEN operador IN ('', '+') THEN rn WHEN operador = '-' THEN -rn ELSE 0 END) as real_rn,
                
                SUM(CASE WHEN operador = '' THEN extra_50 ELSE 0 END) as prog_50,
                SUM(CASE WHEN operador IN ('', '+') THEN extra_50 WHEN operador = '-' THEN -extra_50 ELSE 0 END) as real_50,
                
                SUM(CASE WHEN operador = '' THEN extra_100 ELSE 0 END) as prog_100,
                SUM(CASE WHEN operador IN ('', '+') THEN extra_100 WHEN operador = '-' THEN -extra_100 ELSE 0 END) as real_100
            FROM programacion_diaria
            WHERE fecha BETWEEN '{f_ini}' AND '{f_fin}'
        '''
        df_metrics = pd.read_sql_query(query_metrics, conn)
        m = df_metrics.iloc[0]
        
        query_faltas = f"SELECT COUNT(*) FROM programacion_diaria WHERE novedad = '1' AND operador = '-' AND fecha BETWEEN '{f_ini}' AND '{f_fin}'"
        faltas = pd.read_sql_query(query_faltas, conn).iloc[0,0]
        
        st.metric("🚨 Ocurrencias de Faltas en el Periodo", faltas, help="Veces que se utilizó la novedad '1 - FALTA' en este periodo")
        
        st.write("**Total Global Clientes: Programado vs Real**")
        cm1, cm2, cm3, cm4 = st.columns(4)
        
        r_h = float(m['real_hrs'] or 0)
        p_h = float(m['prog_hrs'] or 0)
        cm1.metric("Hrs Regulares", f"{r_h:g}", f"Prog: {p_h:g}  (Dif: {r_h - p_h:g})", delta_color="off")
        
        r_rn = float(m['real_rn'] or 0)
        p_rn = float(m['prog_rn'] or 0)
        cm2.metric("Recargo Nocturno", f"{r_rn:g}", f"Prog: {p_rn:g}  (Dif: {r_rn - p_rn:g})", delta_color="off")
        
        r_50 = float(m['real_50'] or 0)
        p_50 = float(m['prog_50'] or 0)
        cm3.metric("Extras 50%", f"{r_50:g}", f"Prog: {p_50:g}  (Dif: {r_50 - p_50:g})", delta_color="off")
        
        r_100 = float(m['real_100'] or 0)
        p_100 = float(m['prog_100'] or 0)
        cm4.metric("Extras 100%", f"{r_100:g}", f"Prog: {p_100:g}  (Dif: {r_100 - p_100:g})", delta_color="inverse")
        
        st.divider()
        
        st.write("🏆 **Ranking de Clientes: Mayor Descuadre en Horas 100% (Desviación)**")
        query_ranking = f'''
            SELECT 
                c.nombre as Cliente,
                SUM(CASE WHEN pd.operador = '' THEN pd.extra_100 ELSE 0 END) as 'Prog_100',
                SUM(CASE WHEN pd.operador IN ('', '+') THEN pd.extra_100 WHEN pd.operador = '-' THEN -pd.extra_100 ELSE 0 END) as 'Real_100',
                (SUM(CASE WHEN pd.operador IN ('', '+') THEN pd.extra_100 WHEN pd.operador = '-' THEN -pd.extra_100 ELSE 0 END) - 
                 SUM(CASE WHEN pd.operador = '' THEN pd.extra_100 ELSE 0 END)) as 'Diferencia'
            FROM programacion_diaria pd
            JOIN guardias g ON pd.guardia_id = g.id
            JOIN puestos p ON (CASE WHEN pd.puesto_id > 0 THEN pd.puesto_id ELSE g.puesto_id END) = p.id
            JOIN clientes c ON p.cliente_id = c.id
            WHERE pd.fecha BETWEEN '{f_ini}' AND '{f_fin}'
            GROUP BY c.id
            ORDER BY ABS(Diferencia) DESC
        '''
        df_ranking = pd.read_sql_query(query_ranking, conn)
        if not df_ranking.empty:
            df_ranking['Prog_100'] = df_ranking['Prog_100'].astype(float).round(2)
            df_ranking['Real_100'] = df_ranking['Real_100'].astype(float).round(2)
            df_ranking['Diferencia'] = df_ranking['Diferencia'].astype(float).round(2)
            
            st.dataframe(df_ranking.rename(columns={'Prog_100': 'Programado (Hrs)', 'Real_100': 'Real Contabilizado (Hrs)', 'Diferencia': 'Desviación'}), use_container_width=True, hide_index=True)
        else:
            st.info("No hay datos operativos generados para este periodo.")
    else:
        st.info("Crea un periodo en PT01 para visualizar los indicadores gerenciales.")

# ------------------------------------------
# MÓDULO 0.1: RRHH - MAESTRO DE PERSONAL
# ------------------------------------------
elif menu == "👥 Maestro de Personal (RRHH)":
    st.title("Gestión del Capital Humano")
    
    tab_ficha, tab_sueldos, tab_cargos = st.tabs(["Ficha de Empleado (Ingreso/Salida)", "Histórico de Sueldos", "Configuración de Cargos"])
    
    with tab_cargos:
        with st.form("form_cargo"):
            st.write("**Crear Nuevo Cargo**")
            nom_cargo = st.text_input("Nombre del Cargo").upper()
            if st.form_submit_button("Guardar Cargo"):
                if nom_cargo:
                    try:
                        c.execute("INSERT INTO cargos (nombre) VALUES (?)", (nom_cargo,))
                        conn.commit()
                        st.success("Cargo creado.")
                        st.rerun()
                    except: st.error("El cargo ya existe.")
                else: st.warning("Escriba un nombre.")
        
        df_c = pd.read_sql_query("SELECT id, nombre FROM cargos", conn)
        if not df_c.empty:
            st.dataframe(df_c, hide_index=True)
            
    with tab_ficha:
        st.subheader("Registro y Modificación de Personal")
        df_all_emp = pd.read_sql_query("SELECT * FROM empleados", conn)
        df_cargos = pd.read_sql_query("SELECT nombre FROM cargos", conn)
        df_puestos = pd.read_sql_query("SELECT nombre FROM puestos", conn)
        
        cargos_list = df_cargos['nombre'].tolist() if not df_cargos.empty else []
        puestos_list = df_puestos['nombre'].tolist() if not df_puestos.empty else []
        
        opciones_emp = ["➕ NUEVO EMPLEADO"] + df_all_emp['nui'].tolist() if not df_all_emp.empty else ["➕ NUEVO EMPLEADO"]
        accion_emp = st.selectbox("Seleccionar Empleado a Editar:", opciones_emp)
        
        # Cargar datos si es edición
        v_nui, v_ape, v_nom, v_car, v_cc, v_coor, v_prov, v_ciu, v_fsal, v_est = "", "", "", 0, 0, "", 0, 0, None, "Activo"
        if accion_emp != "➕ NUEVO EMPLEADO":
            emp_data = df_all_emp[df_all_emp['nui'] == accion_emp].iloc[0]
            v_nui = emp_data['nui']
            v_ape = emp_data['apellidos']
            v_nom = emp_data['nombres']
            v_car = cargos_list.index(emp_data['cargo']) if emp_data['cargo'] in cargos_list else 0
            v_cc = puestos_list.index(emp_data['centro_costo']) if emp_data['centro_costo'] in puestos_list else 0
            v_coor = emp_data['coordinador']
            v_prov = list(ecuador.keys()).index(emp_data['provincia']) if emp_data['provincia'] in ecuador else 0
            v_ciu = emp_data['ciudad']
            v_fsal = datetime.strptime(emp_data['f_salida'], "%Y-%m-%d") if emp_data['f_salida'] else None
            v_est = emp_data['estado']
            
            st.info(f"Editando a: **{v_ape} {v_nom}** | Estado: **{v_est}**")
            
        with st.form("form_empleado"):
            col1, col2 = st.columns(2)
            with col1:
                nui = st.text_input("NUI (Cédula de 10 dígitos)*", value=v_nui, max_chars=10, disabled=(accion_emp != "➕ NUEVO EMPLEADO"))
                apellidos = st.text_input("Apellidos*", value=v_ape).upper()
                nombres = st.text_input("Nombres*", value=v_nom).upper()
                cargo = st.selectbox("Cargo", cargos_list, index=v_car if cargos_list else 0)
                
                st.divider()
                st.write("**Información Salarial Inicial / Actualización**")
                sueldo = st.number_input("Sueldo Base ($)", min_value=0.0, value=460.0, step=10.0)
                f_ini_s = st.date_input("Fecha Inicio de este Sueldo")
                
            with col2:
                centro_costo = st.selectbox("Centro de Costo Base", puestos_list, index=v_cc if puestos_list else 0)
                coordinador = st.text_input("Coordinador Asignado", value=v_coor).upper()
                provincia = st.selectbox("Provincia", list(ecuador.keys()), index=v_prov)
                
                ciu_disp = ecuador.get(provincia, ["Guayaquil"])
                idx_ciu = ciu_disp.index(v_ciu) if v_ciu in ciu_disp else 0
                ciudad = st.selectbox("Ciudad", ciu_disp, index=idx_ciu)
                
                st.divider()
                st.write("🛑 **Cese de Funciones**")
                marcar_salida = st.checkbox("¿Registrar Salida/Inactivación?", value=(v_est == 'Inactivo'))
                f_salida = st.date_input("Fecha de Salida Efectiva", value=v_fsal if v_fsal else datetime.today()) if marcar_salida else None
                
            if st.form_submit_button("💾 Guardar Ficha de Empleado", type="primary"):
                if len(nui) == 10 and nui.isdigit() and apellidos and nombres:
                    estado_final = 'Inactivo' if marcar_salida else 'Activo'
                    str_salida = str(f_salida) if f_salida else ""
                    
                    if accion_emp == "➕ NUEVO EMPLEADO":
                        try:
                            c.execute('''INSERT INTO empleados (nui, apellidos, nombres, cargo, centro_costo, coordinador, provincia, ciudad, f_salida, estado)
                                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                      (nui, apellidos, nombres, cargo, centro_costo, coordinador, provincia, ciudad, str_salida, estado_final))
                            # Guardar primer sueldo
                            c.execute("INSERT INTO historico_sueldos (empleado_nui, sueldo, f_inicio, f_fin) VALUES (?, ?, ?, ?)",
                                      (nui, sueldo, str(f_ini_s), ""))
                            conn.commit()
                            st.success("Empleado creado correctamente.")
                            st.rerun()
                        except sqlite3.IntegrityError:
                            st.error("Este NUI ya se encuentra registrado.")
                    else:
                        c.execute('''UPDATE empleados SET apellidos=?, nombres=?, cargo=?, centro_costo=?, coordinador=?, provincia=?, ciudad=?, f_salida=?, estado=?
                                     WHERE nui=?''',
                                  (apellidos, nombres, cargo, centro_costo, coordinador, provincia, ciudad, str_salida, estado_final, nui))
                        
                        # Actualizamos histórico de sueldos si es un sueldo nuevo
                        c.execute("SELECT sueldo FROM historico_sueldos WHERE empleado_nui=? ORDER BY f_inicio DESC LIMIT 1", (nui,))
                        res_s = c.fetchone()
                        if not res_s or res_s[0] != sueldo:
                            # Cerramos el anterior
                            c.execute("UPDATE historico_sueldos SET f_fin=? WHERE empleado_nui=? AND f_fin=''", (str(f_ini_s - timedelta(days=1)), nui))
                            # Insertamos nuevo
                            c.execute("INSERT INTO historico_sueldos (empleado_nui, sueldo, f_inicio, f_fin) VALUES (?, ?, ?, ?)",
                                      (nui, sueldo, str(f_ini_s), ""))
                        conn.commit()
                        st.success("Ficha actualizada.")
                        st.rerun()
                else:
                    st.error("Verifique que el NUI tenga exactamente 10 dígitos y que Apellidos/Nombres no estén vacíos.")

    with tab_sueldos:
        st.write("Trazabilidad de cambios de sueldo para cálculos de nómina.")
        df_hs = pd.read_sql_query('''
            SELECT h.id, e.apellidos || ' ' || e.nombres as Empleado, h.sueldo as 'Sueldo ($)', h.f_inicio as 'Desde', h.f_fin as 'Hasta'
            FROM historico_sueldos h
            JOIN empleados e ON h.empleado_nui = e.nui
            ORDER BY e.apellidos, h.f_inicio DESC
        ''', conn)
        if not df_hs.empty:
            st.dataframe(df_hs.drop(columns=['id']), use_container_width=True, hide_index=True)

# ------------------------------------------
# MÓDULO 1: MAESTRO DE CLIENTES (ASIGNACIÓN)
# ------------------------------------------
elif menu == "📁 Maestro de clientes":
    st.title("Maestro de Datos y Estructura Organizativa")
    
    tab_cli, tab_pto, tab_emp, tab_hor = st.tabs(["Organización", "Centros de Costo (Puestos)", "Asignación de Personal", "Esquemas de Horario"])
    
    with tab_cli:
        c.execute("SELECT MAX(CAST(codigo AS INTEGER)) FROM clientes")
        max_cod_resultado = c.fetchone()[0]
        next_cod = str(max_cod_resultado + 1) if max_cod_resultado else "1"

        with st.form("form_cliente"):
            st.write("**Crear Nueva Organización**")
            st.text_input("Código Empresa (Automático)", value=next_cod, disabled=True)
            nom_cliente = st.text_input("Razón Social / Nombre").upper()
            
            if st.form_submit_button("Guardar Organización", type="primary"):
                if nom_cliente:
                    try:
                        c.execute("INSERT INTO clientes (codigo, nombre) VALUES (?, ?)", (next_cod, nom_cliente))
                        conn.commit()
                        st.success(f"Organización '{nom_cliente}' creada exitosamente.")
                        st.rerun()
                    except: 
                        st.error("Error al guardar en la base de datos.")
                else:
                    st.warning("Debe ingresar el nombre de la empresa.")
                    
        st.divider()
        with st.expander("✏️ Editar o Eliminar Organización Existente"):
            df_empresas = pd.read_sql_query("SELECT id, codigo, nombre FROM clientes ORDER BY CAST(codigo AS INTEGER)", conn)
            if not df_empresas.empty:
                st.dataframe(df_empresas[['codigo', 'nombre']].rename(columns={'codigo':'Código', 'nombre':'Razón Social'}), use_container_width=True, hide_index=True)
                
                dicc_emp_edit = dict(zip(df_empresas['codigo'].astype(str) + " - " + df_empresas['nombre'], df_empresas['id']))
                sel_emp_edit = st.selectbox("Seleccione Organización:", list(dicc_emp_edit.keys()))
                emp_edit_id = dicc_emp_edit[sel_emp_edit]
                emp_data = df_empresas[df_empresas['id'] == emp_edit_id].iloc[0]
                
                nuevo_nom = st.text_input("Modificar Razón Social:", value=emp_data['nombre']).upper()
                
                ce1, ce2 = st.columns(2)
                if ce1.button("💾 Actualizar Razón Social"):
                    c.execute("UPDATE clientes SET nombre=? WHERE id=?", (nuevo_nom, emp_edit_id))
                    conn.commit()
                    st.success("Actualizado")
                    st.rerun()
                if ce2.button("🗑️ Eliminar Organización"):
                    c.execute("DELETE FROM clientes WHERE id=?", (emp_edit_id,))
                    conn.commit()
                    st.success("Eliminada.")
                    st.rerun()
            else:
                st.info("Aún no hay empresas creadas.")

    with tab_pto:
        df_clientes = pd.read_sql_query("SELECT id, nombre FROM clientes", conn)
        if not df_clientes.empty:
            dicc_clientes = dict(zip(df_clientes['nombre'], df_clientes['id']))
            sel_cliente_puesto = st.selectbox("Seleccionar Empresa", list(dicc_clientes.keys()))
            cliente_id_sel = dicc_clientes[sel_cliente_puesto]
            
            with st.form("form_puesto"):
                st.write("**Crear Nuevo Centro de Costo (Puesto)**")
                nom_puesto = st.text_input("Denominación del Puesto").upper()
                
                c_pto1, c_pto2, c_pto3, c_pto4 = st.columns(4)
                with c_pto1: provincia = st.selectbox("Provincia", list(ecuador.keys()))
                with c_pto2: ciudad = st.selectbox("Ciudad", ecuador[provincia])
                with c_pto3: hrs_semana_puesto = st.number_input("Hrs/Semana", value=84.0, step=0.5)
                with c_pto4: estado_puesto = st.selectbox("Estado", ["Habilitado", "Deshabilitado"])
                
                if st.form_submit_button("Guardar Centro de Costo", type="primary"):
                    if nom_puesto:
                        c.execute("SELECT MAX(secuencia) FROM puestos WHERE cliente_id=?", (cliente_id_sel,))
                        max_sec = c.fetchone()[0]
                        next_sec = (max_sec or 0) + 1
                        
                        c.execute("INSERT INTO puestos (cliente_id, nombre, provincia, ciudad, horas_semana, secuencia, estado) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  (cliente_id_sel, nom_puesto, provincia, ciudad, float(hrs_semana_puesto), next_sec, estado_puesto))
                        conn.commit()
                        st.success(f"Puesto guardado como Número {next_sec}.")
                        st.rerun()
                    else:
                        st.warning("Ingrese un nombre de puesto.")
                
            st.divider()
            st.write(f"📋 **Centros de Costo en: {sel_cliente_puesto}**")
            df_all_puestos = pd.read_sql_query(f"SELECT id, secuencia as Num, nombre as Puesto, provincia as Prov, ciudad as Ciud, horas_semana as Hrs, estado as Estado FROM puestos WHERE cliente_id = {cliente_id_sel} ORDER BY secuencia", conn)
            
            if not df_all_puestos.empty:
                st.dataframe(df_all_puestos[['Num', 'Puesto', 'Prov', 'Ciud', 'Hrs', 'Estado']], use_container_width=True, hide_index=True)
                
                with st.expander("✏️ Editar o Eliminar Puesto Existente"):
                    dicc_pto_edit = dict(zip(df_all_puestos['Num'].astype(str) + " - " + df_all_puestos['Puesto'], df_all_puestos['id']))
                    sel_pto_edit = st.selectbox("Seleccione el Puesto:", list(dicc_pto_edit.keys()))
                    
                    if sel_pto_edit:
                        pto_edit_id = dicc_pto_edit[sel_pto_edit]
                        pto_data = df_all_puestos[df_all_puestos['id'] == pto_edit_id].iloc[0]
                        
                        edit_nom = st.text_input("Modificar Nombre Puesto", value=pto_data['Puesto']).upper()
                        cc1, cc2, cc3, cc4 = st.columns(4)
                        
                        idx_prov = list(ecuador.keys()).index(pto_data['Prov']) if pto_data['Prov'] in ecuador else 0
                        with cc1: edit_prov = st.selectbox("Provincia", list(ecuador.keys()), index=idx_prov, key="ed_prov")
                        
                        ciu_disp = ecuador.get(edit_prov, ["Guayaquil"])
                        idx_ciu = ciu_disp.index(pto_data['Ciud']) if pto_data['Ciud'] in ciu_disp else 0
                        with cc2: edit_ciu = st.selectbox("Ciudad", ciu_disp, index=idx_ciu, key="ed_ciu")
                        
                        with cc3: edit_hrs = st.number_input("Horas/Semana", value=float(pto_data['Hrs']), step=0.5)
                        
                        idx_est = 0 if pto_data['Estado'] == 'Habilitado' else 1
                        with cc4: edit_estado = st.selectbox("Estado", ["Habilitado", "Deshabilitado"], index=idx_est, key="ed_est")
                        
                        cb1, cb2 = st.columns(2)
                        if cb1.button("💾 Actualizar Puesto"):
                            c.execute("UPDATE puestos SET nombre=?, provincia=?, ciudad=?, horas_semana=?, estado=? WHERE id=?", (edit_nom, edit_prov, edit_ciu, edit_hrs, edit_estado, pto_edit_id))
                            conn.commit()
                            st.success("Puesto actualizado.")
                            st.rerun()
                        if cb2.button("🗑️ Eliminar Puesto"):
                            c.execute("DELETE FROM puestos WHERE id=?", (pto_edit_id,))
                            conn.commit()
                            st.success("Puesto eliminado.")
                            st.rerun()
            else:
                st.info("No hay Centros de Costo.")
        else: st.warning("Cree una Organización primero.")

    with tab_emp:
        df_clientes = pd.read_sql_query("SELECT id, nombre FROM clientes", conn)
        df_periodos = pd.read_sql_query("SELECT id, nombre, estado FROM periodos", conn)
        
        if not df_periodos.empty and not df_clientes.empty:
            st.write("### Asignación segmentada por Empresa y Periodo")
            c_emp1, c_emp2 = st.columns(2)
            with c_emp1:
                dicc_clientes_emp = dict(zip(df_clientes['nombre'], df_clientes['id']))
                sel_cliente_emp = st.selectbox("1. Selecciona la Empresa:", list(dicc_clientes_emp.keys()))
                cliente_id_emp = dicc_clientes_emp[sel_cliente_emp]
            with c_emp2:
                dicc_per_emp = dict(zip(df_periodos['nombre'], df_periodos['id']))
                sel_periodo_emp = st.selectbox("2. Selecciona el Periodo:", list(dicc_per_emp.keys()))
                periodo_id_sel = dicc_per_emp[sel_periodo_emp]
                estado_per_emp = df_periodos[df_periodos['id'] == periodo_id_sel].iloc[0]['estado']
            
            st.divider()
            
            df_puestos = pd.read_sql_query(f"SELECT id, secuencia || ' - ' || nombre || ' (' || horas_semana || 'h/s)' as info, horas_semana FROM puestos WHERE cliente_id = {cliente_id_emp} AND estado='Habilitado' ORDER BY secuencia", conn)
            df_cods = pd.read_sql_query("SELECT DISTINCT nombre_codigo FROM codigos_horario", conn)
            
            # NUEVO: Traer solo empleados activos de la BD RRHH
            df_rrhh = pd.read_sql_query("SELECT nui, apellidos || ' ' || nombres as nombre_comp FROM empleados WHERE estado='Activo'", conn)
            
            col_frm, col_tbl = st.columns([1, 1.5])
            
            with col_frm:
                if not df_puestos.empty and not df_cods.empty and not df_rrhh.empty:
                    dicc_puestos = dict(zip(df_puestos['info'], df_puestos['id']))
                    dicc_rrhh = dict(zip(df_rrhh['nui'] + " - " + df_rrhh['nombre_comp'], df_rrhh['nui']))
                    
                    if estado_per_emp in ['Generado', 'Cerrado']:
                        st.error(f"🔒 El periodo **{sel_periodo_emp}** ya está **{estado_per_emp}**.")
                        st.write("La asignación de personal está **bloqueada** para proteger la integridad de los datos. Si necesitas hacer un cambio base, ve a la pestaña 'Periodos y generación' y limpia la matriz de este periodo primero.")
                    else:
                        with st.form("form_guardia"):
                            st.write(f"**Vincular empleado al periodo: {sel_periodo_emp}**")
                            st.info("💡 Haz clic abajo, teclea el Número de Puesto y presiona `TAB`")
                            
                            sel_puesto = st.selectbox("Centro de Costo (Puestos Habilitados)", list(dicc_puestos.keys()))
                            
                            # NUEVO BUCADOR DE EMPLEADOS
                            sel_trabajador = st.selectbox("Buscar Empleado (NUI - Nombres)", list(dicc_rrhh.keys()))
                            
                            cod_horario = st.selectbox("Esquema de Horario", df_cods['nombre_codigo'].tolist())
                            
                            if st.form_submit_button("Dar de Alta en Periodo"):
                                puesto_sel_id = dicc_puestos[sel_puesto]
                                nui_sel = dicc_rrhh[sel_trabajador]
                                nom_sel = df_rrhh[df_rrhh['nui'] == nui_sel].iloc[0]['nombre_comp']
                                
                                c.execute("SELECT horas_semana FROM puestos WHERE id=?", (puesto_sel_id,))
                                budget_hrs = float(c.fetchone()[0])
                                
                                df_new_cod = obtener_plantilla_codigo(cod_horario)
                                total_hrs_new_schema = sum(str_to_float(x) for x in df_new_cod['hrs'])
                                dias_ciclo_new = len(df_new_cod) if len(df_new_cod) > 0 else 7
                                hrs_new_semanal = (total_hrs_new_schema / dias_ciclo_new) * 7
                                
                                c.execute("SELECT codigo_horario FROM guardias WHERE puesto_id=? AND periodo_id=?", (puesto_sel_id, periodo_id_sel))
                                guardias_actuales = c.fetchall()
                                hrs_existentes_semanal = 0.0
                                for g in guardias_actuales:
                                    df_g_cod = obtener_plantilla_codigo(g[0])
                                    tot_hrs = sum(str_to_float(x) for x in df_g_cod['hrs'])
                                    dias_c = len(df_g_cod) if len(df_g_cod) > 0 else 7
                                    hrs_existentes_semanal += (tot_hrs / dias_c) * 7
                                    
                                if round((hrs_existentes_semanal + hrs_new_semanal), 2) > round(budget_hrs, 2):
                                    st.error(f"🛑 **Límite Excedido:** El Puesto tiene un presupuesto máximo de **{budget_hrs} h/s**.")
                                    st.warning(f"Ya hay **{hrs_existentes_semanal:.2f} h/s** asignadas. Si añades este esquema equivalente a **{hrs_new_semanal:.2f} h/s**, se sobrepasaría el límite.")
                                else:
                                    # Verificamos si el empleado ya está asignado en este mismo periodo para evitar duplicados
                                    c.execute("SELECT id FROM guardias WHERE cedula=? AND periodo_id=?", (nui_sel, periodo_id_sel))
                                    if c.fetchone():
                                        st.error("Este empleado ya se encuentra programado en este periodo en otro puesto.")
                                    else:
                                        c.execute("INSERT INTO guardias (puesto_id, cedula, nombres, codigo_horario, periodo_id) VALUES (?, ?, ?, ?, ?)",
                                                  (puesto_sel_id, nui_sel, nom_sel, cod_horario, periodo_id_sel))
                                        conn.commit()
                                        st.success("Empleado asignado.")
                                        st.rerun()
                elif df_rrhh.empty:
                    st.warning("⚠️ No hay personal Activo. Vaya a 'Maestro de Personal (RRHH)' y registre empleados primero.")
                else: 
                    st.warning("Cree Centros de Costo Habilitados para esta empresa y Esquemas primero.")
            
            with col_tbl:
                st.write(f"**Personal activo en {sel_periodo_emp} - {sel_cliente_emp}**")
                
                df_activos = pd.read_sql_query(f'''
                    SELECT g.id, g.nombres as Empleado, p.secuencia || ' - ' || p.nombre as 'Num/Puesto', g.codigo_horario as Esquema
                    FROM guardias g JOIN puestos p ON g.puesto_id = p.id
                    WHERE g.periodo_id = {periodo_id_sel} AND p.cliente_id = {cliente_id_emp}
                    ORDER BY p.secuencia
                ''', conn)
                
                if not df_activos.empty:
                    st.dataframe(df_activos[['Empleado', 'Num/Puesto', 'Esquema']], use_container_width=True, hide_index=True)
                    
                    if estado_per_emp not in ['Generado', 'Cerrado']:
                        with st.expander("🗑️ Desvincular Empleado del Periodo"):
                            dicc_borrar = dict(zip(df_activos['Empleado'], df_activos['id']))
                            emp_a_borrar = st.selectbox("Seleccione empleado a eliminar:", list(dicc_borrar.keys()))
                            if st.button("Eliminar Asignación"):
                                c.execute(f"DELETE FROM guardias WHERE id={dicc_borrar[emp_a_borrar]}")
                                conn.commit()
                                st.success("Empleado desvinculado correctamente.")
                                st.rerun()
                    else:
                        st.info("⚠️ La opción de desvincular está deshabilitada porque el periodo ya está generado.")
                else:
                    st.info("Aún no hay personal en este periodo para esta empresa.")
        else:
            st.warning("Crea una Empresa y un Periodo en PT01 primero para poder asignar personal.")

    with tab_hor:
        st.write("Gestión Inteligente de Esquemas Fijos y Rotativos.")
        
        codigos_creados_df = pd.read_sql_query("SELECT DISTINCT nombre_codigo FROM codigos_horario", conn)
        lista_codigos = ["➕ CREAR ESQUEMA SEMANAL (Fijo)", "➕ CREAR ESQUEMA ROTATIVO (Ciclos)"] + codigos_creados_df['nombre_codigo'].tolist()
        
        opcion_esquema = st.selectbox("Acción:", lista_codigos)
        
        if opcion_esquema == "➕ CREAR ESQUEMA ROTATIVO (Ciclos)":
            nombre_esquema = st.text_input("Nombre del Esquema (Ej. 4X4X2_GRUPO_A)").upper()
            st.info("💡 La 'Fecha Ancla' es el día exacto en el calendario donde este grupo empieza su 'DÍA 1' del ciclo. Esto permite que la rotación se mantenga perfecta a través de los meses.")
            fecha_ancla = st.date_input("Fecha Ancla (Inicio del Ciclo)", value=datetime.today())
            
            fases = st.number_input("Cantidad de Fases del Ciclo (Ej: 3 para 4xDía, 4xNoche, 2xLibre)", min_value=1, max_value=10, value=3)
            
            cols = st.columns(fases)
            fase_data = []
            for i in range(fases):
                with cols[i]:
                    st.write(f"**Fase {i+1}**")
                    d = st.number_input(f"Días", min_value=1, max_value=30, value=4 if i<2 else 2, key=f"d_{i}")
                    ing = st.text_input("Ingreso", value="07:00" if i==0 else ("19:00" if i==1 else "D"), key=f"i_{i}")
                    sal = st.text_input("Salida", value="19:00" if i==0 else ("07:00" if i==1 else "D"), key=f"s_{i}")
                    fase_data.append((d, ing, sal))
            
            if st.button("🔄 Construir Matriz de Rotación", type="secondary"):
                dias_totales = sum([x[0] for x in fase_data])
                filas = []
                for d, ing, sal in fase_data:
                    for _ in range(d):
                        filas.append({"INGRESO": ing, "SALIDA": sal})
                
                df_rot = pd.DataFrame(filas)
                df_rot.insert(0, "DÍA", [f"DIA {i+1}" for i in range(dias_totales)])
                df_rot["HRS"] = "0"
                df_rot["RN"] = "0"
                df_rot["50%"] = "0"
                df_rot["100%"] = "0"
                
                st.session_state.df_horario = df_rot
                st.session_state.esquema_actual = opcion_esquema
                st.session_state.f_base_save = str(fecha_ancla)
        
        elif opcion_esquema == "➕ CREAR ESQUEMA SEMANAL (Fijo)":
            nombre_esquema = st.text_input("Nombre del Esquema (Ej. SEMANAL_L-V)").upper()
            if "esquema_actual" not in st.session_state or st.session_state.esquema_actual != opcion_esquema:
                st.session_state.esquema_actual = opcion_esquema
                st.session_state.f_base_save = "SEMANAL"
                st.session_state.df_horario = pd.DataFrame({
                    "DÍA": ["LUNES", "MARTES", "MIÉRCOLES", "JUEVES", "VIERNES", "SÁBADO", "DOMINGO"],
                    "INGRESO": ["0", "0", "0", "0", "0", "0", "0"],
                    "SALIDA":  ["0", "0", "0", "0", "0", "0", "0"],
                    "HRS":     ["0", "0", "0", "0", "0", "0", "0"],
                    "RN":      ["0", "0", "0", "0", "0", "0", "0"],
                    "50%":     ["0", "0", "0", "0", "0", "0", "0"],
                    "100%":    ["0", "0", "0", "0", "0", "0", "0"]
                })
        else:
            nombre_esquema = st.text_input("Modificar Nombre (Opcional)", value=opcion_esquema).upper()
            if "esquema_actual" not in st.session_state or st.session_state.esquema_actual != opcion_esquema:
                st.session_state.esquema_actual = opcion_esquema
                df_loaded = obtener_plantilla_codigo(opcion_esquema)
                
                if 'fecha_base' in df_loaded.columns and len(df_loaded)>0 and pd.notna(df_loaded.iloc[0]['fecha_base']) and df_loaded.iloc[0]['fecha_base'] != 'SEMANAL':
                    st.session_state.f_base_save = df_loaded.iloc[0]['fecha_base']
                    st.info(f"🔄 Esquema Rotativo Detectado. Fecha Ancla: {st.session_state.f_base_save} | Ciclo de {len(df_loaded)} días.")
                else:
                    st.session_state.f_base_save = "SEMANAL"
                    
                df_loaded = df_loaded.rename(columns={'dia_nombre': 'DÍA', 'ingreso': 'INGRESO', 'salida': 'SALIDA', 'hrs': 'HRS', 'rn': 'RN', 'extra_50': '50%', 'extra_100': '100%'})[['DÍA', 'INGRESO', 'SALIDA', 'HRS', 'RN', '50%', '100%']]
                df_loaded = df_loaded.replace({"D": "0", "": "0", None: "0"})
                st.session_state.df_horario = df_loaded

        if "df_horario" in st.session_state:
            df_editado = st.data_editor(st.session_state.df_horario, hide_index=True, use_container_width=True)
            
            df_calculado = df_editado.copy()
            for i in range(len(df_calculado)):
                ing = str(df_calculado.loc[i, "INGRESO"]).strip()
                sal = str(df_calculado.loc[i, "SALIDA"]).strip()
                ing_fmt = formatear_hora_input(ing)
                sal_fmt = formatear_hora_input(sal)
                df_calculado.loc[i, "INGRESO"] = ing_fmt
                df_calculado.loc[i, "SALIDA"] = sal_fmt
                hrs_calc = calcular_horas(ing_fmt, sal_fmt)
                if hrs_calc != "0" or (ing_fmt == "0" and sal_fmt == "0"):
                    df_calculado.loc[i, "HRS"] = hrs_calc

            total_hrs = sum(str_to_float(x) for x in df_calculado["HRS"])
            
            c1, c2 = st.columns(2)
            c1.metric("Suma Total Horas (Esquema/Ciclo)", f"{total_hrs:g} h")
            
            if st.button("💾 Guardar Esquema en Base de Datos", type="primary"):
                if nombre_esquema and not nombre_esquema.startswith("➕"):
                    c.execute("DELETE FROM codigos_horario WHERE nombre_codigo = ?", (nombre_esquema,))
                    
                    f_base = st.session_state.get('f_base_save', 'SEMANAL')
                    
                    for index, fila in df_calculado.iterrows():
                        c.execute('''INSERT INTO codigos_horario 
                                     (nombre_codigo, dia_numero, dia_nombre, ingreso, salida, hrs, rn, extra_50, extra_100, fecha_base) 
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                  (nombre_esquema, index, fila["DÍA"], fila["INGRESO"], fila["SALIDA"], 
                                   str(fila["HRS"]), str(fila["RN"]), str(fila["50%"]), str(fila["100%"]), f_base))
                    conn.commit()
                    st.success(f"Esquema '{nombre_esquema}' guardado con éxito.")
                    del st.session_state.esquema_actual
                    st.rerun()
                else:
                    st.error("Proporcione un nombre válido para guardar el esquema.")

# ------------------------------------------
# MÓDULO 2: PERIODOS Y GENERACION
# ------------------------------------------
elif menu == "🌍 Periodos y generacion de la programacion":
    st.title("Periodos y generacion de la programacion")
    
    df_periodos = pd.read_sql_query("SELECT * FROM periodos", conn)

    st.subheader("1. Directorio y Administración de Periodos")
    col_form, col_tabla = st.columns([1, 2.5])

    with col_form:
        with st.form("form_periodo"):
            st.write("**➕ Crear Nuevo Periodo**")
            nombre_periodo = st.text_input("ID Periodo (Ej. PER_03_2026)").upper()
            f_ini_per = st.date_input("Fecha Inicio")
            f_fin_per = st.date_input("Fecha Fin")
            if st.form_submit_button("Crear Periodo"):
                try:
                    c.execute("INSERT INTO periodos (nombre, f_inicio, f_fin) VALUES (?, ?, ?)", (nombre_periodo, str(f_ini_per), str(f_fin_per)))
                    conn.commit()
                    st.success("Periodo creado.")
                    st.rerun()
                except: st.error("Este ID de periodo ya existe.")

    with col_tabla:
        if not df_periodos.empty:
            df_periodos['Año'] = pd.to_datetime(df_periodos['f_inicio']).dt.year
            años_disp = sorted(df_periodos['Año'].unique().tolist(), reverse=True)

            c_y1, c_y2 = st.columns([1, 3])
            with c_y1: 
                año_filtro_tabla = st.selectbox("Filtrar por Año:", años_disp)

            df_filt = df_periodos[df_periodos['Año'] == año_filtro_tabla]
            st.dataframe(df_filt[['nombre', 'f_inicio', 'f_fin', 'estado']], use_container_width=True, hide_index=True)

            with st.expander("⚙️ Administración Rápida (Bloquear, Reabrir, Limpiar Matriz)", expanded=True):
                ca1, ca2, ca3 = st.columns([1.5, 1, 1])
                with ca1:
                    per_admin_nom = st.selectbox("Seleccionar Periodo:", df_filt['nombre'].tolist(), key="sel_admin_per", label_visibility="collapsed")
                
                if per_admin_nom:
                    per_admin_info = df_filt[df_filt['nombre'] == per_admin_nom].iloc[0]
                    estado_admin = per_admin_info['estado']
                    id_admin = per_admin_info['id']
                    
                    with ca2:
                        if estado_admin != 'Cerrado':
                            if st.button("🔒 Bloquear", use_container_width=True, help="Cierra el periodo para evitar modificaciones."):
                                c.execute("UPDATE periodos SET estado='Cerrado' WHERE id=?", (int(id_admin),))
                                conn.commit()
                                st.rerun()
                        else:
                            if st.button("🔓 Reabrir", use_container_width=True, help="Habilita la escritura."):
                                c.execute("UPDATE periodos SET estado='Generado' WHERE id=?", (int(id_admin),))
                                conn.commit()
                                st.rerun()
                    
                    with ca3:
                        if st.button("🧹 Limpiar Matriz", use_container_width=True, help="Borra las horas pero mantiene el periodo y el personal."):
                            query_ids_guardias = f"SELECT id FROM guardias WHERE periodo_id = {id_admin}"
                            c.execute(f"DELETE FROM programacion_diaria WHERE guardia_id IN ({query_ids_guardias})")
                            c.execute(f"DELETE FROM novedades WHERE guardia_ausente_id IN ({query_ids_guardias})")
                            c.execute("UPDATE periodos SET estado='Pendiente' WHERE id=?", (int(id_admin),))
                            conn.commit()
                            st.rerun()
        else:
            st.info("No hay periodos creados aún.")

    st.divider()

    if not df_periodos.empty:
        st.subheader("2. Generacion de la programacion")
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1: 
            dicc_per = dict(zip(df_periodos['nombre'], df_periodos['id']))
            sel_periodo_nom = st.selectbox("Seleccione el Periodo a Procesar:", list(dicc_per.keys()), key="gen_prog")
            per_info = df_periodos[df_periodos['nombre'] == sel_periodo_nom].iloc[0]
        with c2: st.text_input("Inicio", per_info['f_inicio'], disabled=True)
        with c3: st.text_input("Fin", per_info['f_fin'], disabled=True)

        estado_actual = per_info['estado']
        
        if estado_actual in ["Cerrado", "Generado"]:
            st.warning(f"Estado del periodo: **{estado_actual}**. No se puede ejecutar el procesamiento base.")
        else:
            if st.button("🚀 Ejecutar Job de Programación (Masivo)", type="primary"):
                query_vacios = f'''
                    SELECT p.secuencia || ' - ' || p.nombre as puesto_nombre, c.nombre as empresa
                    FROM puestos p
                    JOIN clientes c ON p.cliente_id = c.id
                    WHERE p.estado = 'Habilitado'
                    AND p.id NOT IN (SELECT puesto_id FROM guardias WHERE periodo_id = {per_info['id']})
                '''
                df_vacios = pd.read_sql_query(query_vacios, conn)
                errores_vacios = []
                if not df_vacios.empty:
                    for _, row in df_vacios.iterrows():
                        errores_vacios.append(f"• Empresa **{row['empresa']}** - Puesto **{row['puesto_nombre']}**: Está Habilitado pero NO tiene personal asignado.")
                
                query_val = f'''
                    SELECT p.id as puesto_id, p.secuencia, p.nombre as puesto, p.horas_semana, g.nombres, g.codigo_horario
                    FROM puestos p
                    JOIN guardias g ON g.puesto_id = p.id
                    WHERE g.periodo_id = {per_info['id']}
                '''
                df_val = pd.read_sql_query(query_val, conn)
                errores_cuadre = []
                
                if not df_val.empty:
                    for puesto_id, group in df_val.groupby('puesto_id'):
                        puesto_nombre = str(group.iloc[0]['secuencia']) + " - " + group.iloc[0]['puesto']
                        budget_hrs = float(group.iloc[0]['horas_semana'])
                        suma_asignada = 0.0
                        
                        for _, row in group.iterrows():
                            df_esq = obtener_plantilla_codigo(row['codigo_horario'])
                            tot_hrs = sum(str_to_float(x) for x in df_esq['hrs'])
                            dias_c = len(df_esq) if len(df_esq) > 0 else 7
                            suma_asignada += (tot_hrs / dias_c) * 7
                            
                        if round(suma_asignada, 2) != round(budget_hrs, 2):
                            errores_cuadre.append(f"• **{puesto_nombre}**: Inconsistencia. Presupuesto Puesto: **{budget_hrs}h/sem** | Horarios Asignados Suman Equivalente a: **{suma_asignada:.2f}h/sem**.")

                if errores_vacios or errores_cuadre:
                    st.error("🛑 **Bloqueo de Generación: Se encontraron inconsistencias en la configuración.**")
                    if errores_vacios:
                        st.write("**⚠️ Puestos Habilitados sin Cobertura:** (Deshabilite el puesto en Maestro de clientes si ya no está en operación)")
                        for err in errores_vacios: st.warning(err)
                    if errores_cuadre:
                        st.write("**⚠️ Inconsistencia Presupuestal:** (Las horas asignadas deben ser exactamente iguales a las del Puesto)")
                        for err in errores_cuadre: st.warning(err)
                else:
                    with st.spinner('Procesando y Rolando Datos...'):
                        df_todos_guardias = pd.read_sql_query(f"SELECT id, puesto_id, cedula, nombres, codigo_horario FROM guardias WHERE periodo_id={per_info['id']}", conn)
                        fi = datetime.strptime(per_info['f_inicio'], "%Y-%m-%d")
                        ff = datetime.strptime(per_info['f_fin'], "%Y-%m-%d")
                        
                        for _, g in df_todos_guardias.iterrows():
                            df_cod = obtener_plantilla_codigo(g["codigo_horario"])
                            
                            is_rotative = False
                            base_date = datetime(2026, 1, 1)
                            cycle_len = 7
                            if 'fecha_base' in df_cod.columns and len(df_cod) > 0:
                                f_base_val = df_cod.iloc[0]['fecha_base']
                                if pd.notna(f_base_val) and f_base_val != 'SEMANAL':
                                    is_rotative = True
                                    cycle_len = len(df_cod)
                                    try: base_date = datetime.strptime(f_base_val, "%Y-%m-%d")
                                    except: pass
                            
                            actual = fi
                            while actual <= ff:
                                f_str = actual.strftime("%Y-%m-%d")
                                c.execute("SELECT id FROM programacion_diaria WHERE guardia_id=? AND fecha=? AND operador=''", (g["id"], f_str))
                                if not c.fetchone():
                                    if is_rotative:
                                        dias_diff = (actual - base_date).days
                                        idx = dias_diff % cycle_len
                                        fila_dia = df_cod.loc[df_cod['dia_numero'] == idx]
                                    else:
                                        fila_dia = df_cod.loc[df_cod['dia_numero'] == actual.weekday()]
                                        
                                    if not fila_dia.empty:
                                        ingreso = fila_dia['ingreso'].values[0]
                                        salida = fila_dia['salida'].values[0]
                                        hrs = str_to_float(fila_dia['hrs'].values[0])
                                        rn = str_to_float(fila_dia['rn'].values[0])
                                        e50 = str_to_float(fila_dia['extra_50'].values[0])
                                        e100 = str_to_float(fila_dia['extra_100'].values[0])
                                        
                                        c.execute('''INSERT INTO programacion_diaria (guardia_id, puesto_id, fecha, ingreso, salida, hrs, rn, extra_50, extra_100, operador, novedad) 
                                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "", "0")''', 
                                                  (g["id"], g["puesto_id"], f_str, ingreso, salida, hrs, rn, e50, e100))
                                actual += timedelta(days=1)
                        
                        c.execute("UPDATE periodos SET estado = 'Generado' WHERE id = ?", (int(per_info['id']),))
                        
                        c.execute("SELECT id, nombre FROM periodos WHERE f_inicio > ? ORDER BY f_inicio ASC LIMIT 1", (per_info['f_inicio'],))
                        next_per = c.fetchone()
                        clonados = 0
                        next_per_nom = ""
                        
                        if next_per:
                            next_per_id = next_per[0]
                            next_per_nom = next_per[1]
                            
                            for _, g in df_todos_guardias.iterrows():
                                c.execute("SELECT id FROM guardias WHERE nombres=? AND periodo_id=?", (g['nombres'], next_per_id))
                                if not c.fetchone():
                                    c.execute("INSERT INTO guardias (puesto_id, cedula, nombres, codigo_horario, periodo_id) VALUES (?, ?, ?, ?, ?)",
                                              (g['puesto_id'], g['cedula'], g['nombres'], g['codigo_horario'], next_per_id))
                                    clonados += 1
                                    
                        conn.commit()
                        
                    st.success("✅ ¡Job de Generación ejecutado exitosamente!")
                    if clonados > 0:
                        st.info(f"🔄 **AUTO-ROLLOVER ACTIVADO:** Se copiaron las asignaciones de {clonados} empleados automáticamente al periodo {next_per_nom}.")
                    st.rerun()

        st.divider()
        st.subheader("3. Horarios de la programacion generada")
        st.write("Genera un archivo PDF con la hoja de horarios por empleado, listo para imprimir y firmar.")
        
        df_per_pdf = pd.read_sql_query("SELECT id, nombre FROM periodos WHERE estado IN ('Generado', 'Cerrado')", conn)
        
        if not df_per_pdf.empty:
            dicc_per_pdf = dict(zip(df_per_pdf['nombre'], df_per_pdf['id']))
            sel_periodo_pdf = st.selectbox("Seleccione el Periodo para exportar a PDF:", list(dicc_per_pdf.keys()))
            per_info_pdf = df_periodos[df_periodos['nombre'] == sel_periodo_pdf].iloc[0]
            
            if not HAS_FPDF:
                st.error("⚠️ Falta la librería para generar PDF. Por favor, abre la terminal y ejecuta: `pip install fpdf`")
            else:
                if st.button("📄 Generar PDF de Horarios", type="secondary"):
                    with st.spinner("Compilando el documento PDF, por favor espera..."):
                        query_pdf = f'''
                            SELECT 
                                pd.fecha, pd.ingreso, pd.salida, pd.hrs,
                                g.nombres, g.cedula, 
                                c.nombre as empresa, 
                                p.secuencia || ' - ' || p.nombre as puesto
                            FROM programacion_diaria pd
                            JOIN guardias g ON pd.guardia_id = g.id
                            JOIN puestos p ON (CASE WHEN pd.puesto_id > 0 THEN pd.puesto_id ELSE g.puesto_id END) = p.id
                            JOIN clientes c ON p.cliente_id = c.id
                            WHERE pd.fecha BETWEEN '{per_info_pdf['f_inicio']}' AND '{per_info_pdf['f_fin']}'
                            AND pd.operador = ''
                            ORDER BY g.nombres, pd.fecha
                        '''
                        df_pdf_datos = pd.read_sql_query(query_pdf, conn)
                        
                        if df_pdf_datos.empty:
                            st.warning("No hay horarios registrados para este periodo.")
                        else:
                            try:
                                pdf_bytes = generar_pdf_horarios(df_pdf_datos, sel_periodo_pdf)
                                st.success("¡PDF generado con éxito!")
                                st.download_button(
                                    label="⬇️ Descargar Archivo PDF",
                                    data=pdf_bytes,
                                    file_name=f"Horarios_Originales_{sel_periodo_pdf}.pdf",
                                    mime="application/pdf"
                                )
                            except Exception as e:
                                st.error(f"Ocurrió un error al crear el PDF: {e}")
        else:
            st.info("Primero debes generar un periodo para poder exportar los horarios a PDF.")
            
    else:
        st.info("Crea un periodo para empezar.")

# ------------------------------------------
# MÓDULO 3: TRANSACCIONES DE LA PROGRAMACION
# ------------------------------------------
elif menu == "🔄 Transacciones de la programacion":
    st.title("Hoja de Entrada de Tiempos y Ajustes (CAT2)")
    
    df_clientes_con = pd.read_sql_query("SELECT id, nombre FROM clientes", conn)
    df_per_con = pd.read_sql_query("SELECT * FROM periodos WHERE estado IN ('Generado', 'Cerrado')", conn)
    
    if not df_clientes_con.empty and not df_per_con.empty:
        col_c, col_p, col_pto = st.columns([1, 1, 2])
        with col_c: 
            dicc_cl_con = dict(zip(df_clientes_con['nombre'], df_clientes_con['id']))
            cliente_con = st.selectbox("1. Empresa:", list(dicc_cl_con.keys()))
        with col_p:
            dicc_per_con = dict(zip(df_per_con['nombre'], df_per_con['id']))
            periodo_con = st.selectbox("2. Periodo:", list(dicc_per_con.keys()))
            
        p_inf = df_per_con[df_per_con['nombre'] == periodo_con].iloc[0]
        f_ini_c = p_inf['f_inicio']
        f_fin_c = p_inf['f_fin']
        estado_per_actual = p_inf['estado']
        
        with col_pto:
            df_puestos_cliente = pd.read_sql_query(f"SELECT id, secuencia || ' - ' || nombre as nombre_sec FROM puestos WHERE cliente_id={dicc_cl_con[cliente_con]} ORDER BY secuencia", conn)
            if not df_puestos_cliente.empty:
                dicc_pto_cat2 = dict(zip(df_puestos_cliente['nombre_sec'], df_puestos_cliente['id']))
                puesto_con_sec = st.selectbox("3. Centro de Costo (Puesto):", list(dicc_pto_cat2.keys()))
                puesto_con_id = dicc_pto_cat2[puesto_con_sec]
            else:
                st.warning("Esta empresa no tiene puestos.")
                puesto_con_sec = None
                puesto_con_id = 0
                
        st.divider()
        
        if puesto_con_sec:
            st.subheader(f"📅 Personal en: {puesto_con_sec}")
            
            query_matriz = f'''
                SELECT g.nombres as 'EMPLEADO', pd.fecha,
                       SUM(CASE WHEN pd.operador = '-' THEN -pd.hrs ELSE pd.hrs END) as HRS_DIA
                FROM programacion_diaria pd
                JOIN guardias g ON pd.guardia_id = g.id
                JOIN puestos p ON (CASE WHEN pd.puesto_id > 0 THEN pd.puesto_id ELSE g.puesto_id END) = p.id
                WHERE p.cliente_id = {dicc_cl_con[cliente_con]} 
                AND p.id = {puesto_con_id}
                AND pd.fecha BETWEEN '{f_ini_c}' AND '{f_fin_c}'
                GROUP BY g.nombres, pd.fecha
            '''
            df_m = pd.read_sql_query(query_matriz, conn)
            
            if not df_m.empty:
                pivot_df = df_m.pivot_table(index='EMPLEADO', columns='fecha', values='HRS_DIA', aggfunc='sum', fill_value=0)
                pivot_visual = pivot_df.replace(0, "") 
                pivot_df_reset = pivot_visual.reset_index()
                
                evento_seleccion = st.dataframe(pivot_df_reset, use_container_width=True, hide_index=True, on_select="rerun", selection_mode="single-row")
                
                st.divider()
                
                if estado_per_actual == "Cerrado":
                    st.error("🔒 El periodo está CERRADO. Matriz de solo lectura.")
                else:
                    filas_seleccionadas = evento_seleccion.selection.rows
                    if filas_seleccionadas:
                        indice_fila = filas_seleccionadas[0]
                        guardia_ausente = pivot_df_reset.iloc[indice_fila]['EMPLEADO']
                        
                        st.subheader(f"🛠️ Gestión de Turnos: {guardia_ausente}")
                        
                        c.execute(f"SELECT id FROM guardias WHERE nombres=? AND periodo_id={p_inf['id']}", (guardia_ausente,))
                        res_aus = c.fetchone()
                        ausente_id = res_aus[0] if res_aus else 0
                        
                        tab_nov, tab_edit = st.tabs(["🔴 Registrar Novedad / Reemplazo", "✏️ Modificación global de horas"])

                        with tab_nov:
                            st.write("**1. Registro de Ausencia / Novedad**")
                            c_dias, c_mot = st.columns([1, 1])
                            with c_dias: fechas_rango = st.date_input("Rango de Validez", [], min_value=datetime.strptime(f_ini_c, "%Y-%m-%d"), max_value=datetime.strptime(f_fin_c, "%Y-%m-%d"))
                            with c_mot: 
                                novedad_seleccionada = st.selectbox("Código Novedad (Motivo)", list(diccionario_novedades.keys()))
                                codigo_novedad_ausente = diccionario_novedades[novedad_seleccionada]
                                
                            f_str_eval = fechas_rango[0].strftime("%Y-%m-%d") if len(fechas_rango) > 0 else None
                            
                            datos_aus = None
                            if f_str_eval:
                                c.execute("SELECT ingreso, salida, hrs, rn, extra_50, extra_100 FROM programacion_diaria WHERE guardia_id=? AND fecha=? AND operador=''", (ausente_id, f_str_eval))
                                datos_aus = c.fetchone()
                                
                            default_ini = datos_aus[0] if datos_aus else "07:00"
                            default_fin = datos_aus[1] if datos_aus else "19:00"
                            
                            st.write("**Especificar Horario de la Novedad:**")
                            c_t1, c_t2 = st.columns(2)
                            with c_t1: hora_ini_aus = st.text_input("Hora Inicio Ausencia", default_ini)
                            with c_t2: hora_fin_aus = st.text_input("Hora Fin Ausencia", default_fin)
                            
                            ing_eval = formatear_hora_input(hora_ini_aus)
                            sal_eval = formatear_hora_input(hora_fin_aus)
                            
                            st.divider()
                            
                            c_left, c_right = st.columns(2)
                            bloquear_guardado = False
                                
                            with c_left:
                                st.write(f"🔴 **Desglose Original de: {guardia_ausente}**")
                                if datos_aus:
                                    st.info(f"**Turno Base:** {datos_aus[0]} a {datos_aus[1]} \n\n **HRS:** {datos_aus[2]} | **RN:** {datos_aus[3]} | **50%:** {datos_aus[4]} | **100%:** {datos_aus[5]}")
                                else:
                                    st.warning("Seleccione una fecha de validez arriba para ver el turno programado.")
                                    
                            with c_right:
                                st.write("🟢 **Reemplazado Por (Opcional)**")
                                df_todos = pd.read_sql_query(f"SELECT id, nombres FROM guardias WHERE periodo_id={p_inf['id']}", conn)
                                dicc_todos = dict(zip(df_todos['nombres'], df_todos['id']))
                                
                                opcion_sin_reemplazo = "-- NINGUNO (Solo registrar ausencia) --"
                                opciones_reemplazo = [opcion_sin_reemplazo] + [n for n in dicc_todos.keys() if n != guardia_ausente]
                                
                                reemplazo_nom = st.selectbox("Buscar Empleado:", opciones_reemplazo, label_visibility="collapsed")
                                
                                reemplazo_id = None
                                if reemplazo_nom != opcion_sin_reemplazo:
                                    reemplazo_id = dicc_todos[reemplazo_nom]
                                    if f_str_eval:
                                        c.execute("SELECT ingreso, salida, hrs, rn, extra_50, extra_100 FROM programacion_diaria WHERE guardia_id=? AND fecha=? AND operador IN ('', '+')", (reemplazo_id, f_str_eval))
                                        turnos_remp = c.fetchall()
                                        
                                        if turnos_remp:
                                            cruce = False
                                            turno_cruzado = None
                                            for tr in turnos_remp:
                                                if hay_cruce_horarios(ing_eval, sal_eval, tr[0], tr[1]):
                                                    cruce = True
                                                    turno_cruzado = tr
                                                    break
                                            
                                            if cruce:
                                                st.error(f"**🛑 CRUCE DETECTADO:** Ya tiene turno de {turno_cruzado[0]} a {turno_cruzado[1]}. No puede cubrir de {ing_eval} a {sal_eval}.")
                                                bloquear_guardado = True
                                            else:
                                                st.success(f"✅ Tiene otro turno ({turnos_remp[0][0]} a {turnos_remp[0][1]}), pero NO choca. Puede cubrir.")
                                        else:
                                            st.success(f"✅ **{reemplazo_nom} está totalmente LIBRE este día.**")
                                else:
                                    st.info("ℹ️ No se asignará reemplazo. Las horas seleccionadas solo se descontarán del ausente.")
                                    
                            st.divider()
                            st.write("**3. Ajuste Final de Horas**")
                            st.caption("Las horas se recalculan automáticamente si modificas Inicio/Fin arriba. Puedes editarlas manualmente si es necesario.")
                            
                            v_h_calc = float(calcular_horas(ing_eval, sal_eval))
                            v_h_default = v_h_calc if v_h_calc > 0 else (float(datos_aus[2]) if datos_aus else 12.0)
                            v_rn_default = float(datos_aus[3]) if datos_aus else 0.0
                            v_50_default = float(datos_aus[4]) if datos_aus else 4.0
                            v_100_default = float(datos_aus[5]) if datos_aus else 0.0
                            
                            key_suffix = f"{ing_eval}_{sal_eval}"
                            
                            c_desc, c_asig = st.columns(2)
                            with c_desc:
                                st.write(f"🔻 **A DESCONTAR ({guardia_ausente})**")
                                c_d1, c_d2 = st.columns(2)
                                with c_d1: 
                                    desc_h = st.number_input("Descontar Hrs", value=v_h_default, key=f"dh_{key_suffix}")
                                    desc_50 = st.number_input("Descontar 50%", value=v_50_default, key=f"d50_{key_suffix}")
                                with c_d2:
                                    desc_rn = st.number_input("Descontar RN", value=v_rn_default, key=f"drn_{key_suffix}")
                                    desc_100 = st.number_input("Descontar 100%", value=v_100_default, key=f"d100_{key_suffix}")
                                    
                            with c_asig:
                                if reemplazo_id:
                                    st.write(f"🔺 **A PAGAR ({reemplazo_nom})**")
                                    c_a1, c_a2 = st.columns(2)
                                    with c_a1: 
                                        asig_h = st.number_input("Pagar Hrs", value=v_h_default, key=f"ah_{key_suffix}")
                                        asig_50 = st.number_input("Pagar 50%", value=v_50_default, key=f"a50_{key_suffix}")
                                    with c_a2:
                                        asig_rn = st.number_input("Pagar RN", value=v_rn_default, key=f"arn_{key_suffix}")
                                        asig_100 = st.number_input("Pagar 100%", value=v_100_default, key=f"a100_{key_suffix}")
                                else:
                                    st.write("🔺 **A PAGAR (Reemplazo)**")
                                    st.caption("No aplica, se eligió: SIN REEMPLAZO.")
                                    asig_h, asig_rn, asig_50, asig_100 = 0.0, 0.0, 0.0, 0.0

                            if st.button("✅ Procesar y Contabilizar Novedad", type="primary", use_container_width=True, disabled=bloquear_guardado):
                                if len(fechas_rango) == 2:
                                    fi_r, ff_r = fechas_rango
                                    codigo_novedad_reemplazo = f"{codigo_novedad_ausente}.1"
                                    
                                    while fi_r <= ff_r:
                                        f_str = fi_r.strftime("%Y-%m-%d")
                                        c.execute("SELECT id FROM programacion_diaria WHERE guardia_id=? AND fecha=? AND operador=''", (ausente_id, f_str))
                                        has_orig = c.fetchone()
                                        
                                        if has_orig:
                                            c.execute('''INSERT INTO programacion_diaria (guardia_id, puesto_id, fecha, ingreso, salida, hrs, rn, extra_50, extra_100, operador, novedad) 
                                                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "-", ?)''', 
                                                      (ausente_id, puesto_con_id, f_str, ing_eval, sal_eval, desc_h, desc_rn, desc_50, desc_100, codigo_novedad_ausente))
                                            
                                            if reemplazo_id:
                                                c.execute('''INSERT INTO programacion_diaria (guardia_id, puesto_id, fecha, ingreso, salida, hrs, rn, extra_50, extra_100, operador, novedad) 
                                                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "+", ?)''', 
                                                          (reemplazo_id, puesto_con_id, f_str, ing_eval, sal_eval, asig_h, asig_rn, asig_50, asig_100, codigo_novedad_reemplazo))
                                                
                                                c.execute("INSERT INTO novedades (guardia_ausente_id, guardia_reemplazo_id, fecha, tipo, motivo) VALUES (?, ?, ?, ?, ?)",
                                                          (ausente_id, reemplazo_id, f_str, "Reemplazo", novedad_seleccionada))
                                            else:
                                                c.execute("INSERT INTO novedades (guardia_ausente_id, guardia_reemplazo_id, fecha, tipo, motivo) VALUES (?, ?, ?, ?, ?)",
                                                          (ausente_id, 0, f_str, "Ausencia Sin Reemplazo", novedad_seleccionada))
                                            
                                        fi_r += timedelta(days=1)
                                    conn.commit()
                                    st.success(f"¡Éxito! Movimiento guardado correctamente.")
                                    st.rerun()
                                else:
                                    st.error("Seleccione un rango de fechas de validez completo (Día Inicio y Día Fin).")

                        with tab_edit:
                            df_todos_mod = pd.read_sql_query(f"SELECT id, nombres FROM guardias WHERE periodo_id={p_inf['id']}", conn)
                            dicc_todos_mod = dict(zip(df_todos_mod['nombres'], df_todos_mod['id']))
                            lista_mod = list(dicc_todos_mod.keys())
                            idx_mod = lista_mod.index(guardia_ausente) if guardia_ausente in lista_mod else 0
                            
                            emp_mod_global = st.selectbox("Seleccionar Empleado para modificar sus horas:", lista_mod, index=idx_mod)
                            emp_mod_id = dicc_todos_mod[emp_mod_global]
                            
                            st.info(f"Editando horas de **{emp_mod_global}** asignadas al cliente/puesto actual (**{puesto_con_sec}**). Edite las celdas directamente y presione Guardar.")
                            
                            d_ini = datetime.strptime(f_ini_c, "%Y-%m-%d")
                            d_fin = datetime.strptime(f_fin_c, "%Y-%m-%d")
                            dias_totales = (d_fin - d_ini).days + 1
                            
                            fechas_str = [(d_ini + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(dias_totales)]
                            fechas_headers = [(d_ini + timedelta(days=i)).strftime("%d-%m") for i in range(dias_totales)]
                            
                            c.execute(f"SELECT fecha, hrs, rn, extra_50, extra_100 FROM programacion_diaria WHERE guardia_id={emp_mod_id} AND puesto_id={puesto_con_id} AND fecha BETWEEN '{f_ini_c}' AND '{f_fin_c}' AND operador != '-'")
                            records = c.fetchall()
                            data_dict = {r[0]: (float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in records}
                            
                            row_hrs, row_rn, row_50, row_100 = [], [], [], []
                            for d in fechas_str:
                                if d in data_dict:
                                    row_hrs.append(data_dict[d][0])
                                    row_rn.append(data_dict[d][1])
                                    row_50.append(data_dict[d][2])
                                    row_100.append(data_dict[d][3])
                                else:
                                    row_hrs.append(0.0)
                                    row_rn.append(0.0)
                                    row_50.append(0.0)
                                    row_100.append(0.0)
                                    
                            df_grid = pd.DataFrame({"CONCEPTO": ["Hrs Regulares", "Recargo Nocturno", "Extra 50%", "Extra 100%"]})
                            for i, header in enumerate(fechas_headers):
                                df_grid[header] = [row_hrs[i], row_rn[i], row_50[i], row_100[i]]
                                
                            edited_grid = st.data_editor(df_grid, hide_index=True, use_container_width=True)
                            
                            if st.button("💾 Guardar Hoja de Tiempos Manual", type="primary"):
                                with st.spinner("Guardando en la base de datos..."):
                                    for i, d in enumerate(fechas_str):
                                        header = fechas_headers[i]
                                        val_hrs = float(edited_grid.loc[0, header])
                                        val_rn  = float(edited_grid.loc[1, header])
                                        val_50  = float(edited_grid.loc[2, header])
                                        val_100 = float(edited_grid.loc[3, header])
                                        
                                        c.execute(f"SELECT id FROM programacion_diaria WHERE guardia_id={emp_mod_id} AND fecha='{d}' AND puesto_id={puesto_con_id} AND operador != '-'")
                                        row_exist = c.fetchone()
                                        
                                        if row_exist:
                                            c.execute(f"UPDATE programacion_diaria SET hrs=?, rn=?, extra_50=?, extra_100=? WHERE id=?",
                                                      (val_hrs, val_rn, val_50, val_100, row_exist[0]))
                                        else:
                                            if val_hrs > 0 or val_rn > 0 or val_50 > 0 or val_100 > 0:
                                                c.execute('''INSERT INTO programacion_diaria (guardia_id, puesto_id, fecha, ingreso, salida, hrs, rn, extra_50, extra_100, operador, novedad)
                                                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "+", "8")''',
                                                          (emp_mod_id, puesto_con_id, d, "MANUAL", "MANUAL", val_hrs, val_rn, val_50, val_100))
                                    conn.commit()
                                st.success("Hoja de tiempos actualizada correctamente.")
                                st.rerun()
                    else:
                        st.info("👈 Haz clic en la casilla de un empleado en la matriz para gestionar.")
            else:
                st.info("No hay programación generada para este Centro de Costo en este periodo.")

# ------------------------------------------
# MÓDULO 4: REPORTES Y NÓMINA (ZREP)
# ------------------------------------------
elif menu == "📥 Reportes":
    st.title("Extracción de Datos y Auditoría (ZREP)")
    
    tab_export, tab_audit = st.tabs(["📥 Exportación a SAP/HCM", "🔍 Log de Auditoría Operativa"])
    
    with tab_export:
        col1, col2 = st.columns(2)
        with col1: fecha_export_ini = st.date_input("Fecha Inicio Evaluación:")
        with col2: fecha_export_fin = st.date_input("Fecha Fin Evaluación:")
        
        if st.button("Ejecutar Extracción", type="primary", use_container_width=True):
            query_export = f'''
                SELECT 
                    pd.fecha, pd.ingreso, pd.salida, pd.hrs, pd.rn, pd.extra_50, pd.extra_100, pd.operador, pd.novedad,
                    g.cedula, g.nombres, g.codigo_horario, g.id as id_empleado,
                    c.id as id_cliente, c.nombre as cliente_nombre, 
                    p.nombre as puesto_referencia, p.secuencia as num_secuencia
                FROM programacion_diaria pd
                JOIN guardias g ON pd.guardia_id = g.id
                JOIN puestos p ON (CASE WHEN pd.puesto_id > 0 THEN pd.puesto_id ELSE g.puesto_id END) = p.id
                JOIN clientes c ON p.cliente_id = c.id
                WHERE pd.fecha BETWEEN '{fecha_export_ini}' AND '{fecha_export_fin}' AND pd.hrs > 0
            '''
            df_export = pd.read_sql_query(query_export, conn)
            
            if df_export.empty: st.warning("No existen documentos.")
            else:
                mask_ausente = df_export['operador'] == '-'
                for col in ['hrs', 'rn', 'extra_50', 'extra_100']:
                    df_export.loc[mask_ausente, col] = df_export.loc[mask_ausente, col].astype(float) * -1
                    
                df_final = pd.DataFrame()
                df_final["htd_fecha"] = df_export["fecha"]
                df_final["htd_hora_entra"] = df_export["ingreso"].apply(formato_hora_csv)
                df_final["htd_hora_sale"] = df_export["salida"].apply(formato_hora_csv)
                
                df_final["htd_horas_regulares"] = df_export["hrs"].apply(lambda x: f"{float(x):g}")
                df_final["htd_recargo_nocturno"] = df_export["rn"].apply(lambda x: f"{float(x):g}")
                df_final["htd_horas_50"] = df_export["extra_50"].apply(lambda x: f"{float(x):g}")
                df_final["htd_horas_100"] = df_export["extra_100"].apply(lambda x: f"{float(x):g}")
                
                df_final["htd_horas_feriado"] = 0
                df_final["ch_horario"] = df_export["codigo_horario"]
                df_final["re_empleado"] = df_export["id_empleado"]
                df_final["re_apellidos"] = df_export["nombres"]
                df_final["re_nombres"] = df_export["nombres"]
                
                df_final["cd_secuencia"] = df_export["num_secuencia"]
                
                df_final["cc_cliente"] = df_export["id_cliente"]
                df_final["cc_nombre"] = df_export["cliente_nombre"]
                df_final["htd_operador"] = df_export["operador"] 
                df_final["htd_novedad"] = df_export["novedad"]
                df_final["cc_referencia"] = df_export["puesto_referencia"]
                df_final["re_cedula"] = df_export["cedula"]
                df_final["rr_regional"] = 1
                df_final["LNE"] = 1

                st.dataframe(df_final.head(15), use_container_width=True)
                csv_data = df_final.to_csv(index=False).encode('utf-8')
                st.download_button("⬇️ Descargar Layout TXT/CSV", data=csv_data, file_name=f"Interfaz_HCM_{fecha_export_ini}_al_{fecha_export_fin}.csv", mime="text/csv")
                
    with tab_audit:
        st.subheader("🔍 Historial Completo de Reemplazos y Novedades")
        df_ultimas_nov = pd.read_sql_query('''
            SELECT n.fecha as Fecha, g1.nombres as 'Empleado Ausente', 
                   IFNULL(g2.nombres, '--- SIN REEMPLAZO ---') as 'Reemplazo Asignado', 
                   n.motivo as 'Novedad Registrada'
            FROM novedades n
            JOIN guardias g1 ON n.guardia_ausente_id = g1.id
            LEFT JOIN guardias g2 ON n.guardia_reemplazo_id = g2.id
            ORDER BY n.id DESC
        ''', conn)
        if not df_ultimas_nov.empty:
            st.dataframe(df_ultimas_nov, use_container_width=True, hide_index=True)
        else:
            st.info("No hay transacciones recientes en el log de auditoría.")