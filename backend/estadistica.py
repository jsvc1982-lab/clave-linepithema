"""Estadística del estudio Linepithema.

Solo biblioteca estándar de Python (sin numpy ni scipy): así funciona en Render sin instalar nada nuevo.
Cada función fue validada contra scipy / statsmodels (ver pruebas). Unidad de análisis principal: el ESPÉCIMEN identificado
(primer intento), agrupado por participante (cada persona aporta hasta 2 especímenes, que no son independientes entre sí).
"""
import math
import random
import statistics
import time

GRUPOS = ['2D', '2D_META', '3D', '3D_META']
Z95 = statistics.NormalDist().inv_cdf(0.975)


# ======================================================================================================================
# Distribuciones
# ======================================================================================================================
def norm_sf(z):
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _gammq(a, x):
    """Gamma incompleta regularizada superior Q(a, x)."""
    if x <= 0:
        return 1.0
    if x < a + 1.0:
        ap, s = a, 1.0 / a
        d = s
        for _ in range(2000):
            ap += 1.0
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-16:
                break
        return max(0.0, 1.0 - s * math.exp(-x + a * math.log(x) - math.lgamma(a)))
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 2000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-16:
            break
    return min(1.0, math.exp(-x + a * math.log(x) - math.lgamma(a)) * h)


def chi2_sf(x, df):
    return _gammq(df / 2.0, x / 2.0) if x > 0 else 1.0


# ======================================================================================================================
# Descriptivos
# ======================================================================================================================
def wilson(k, n, conf=0.95):
    """Intervalo de Wilson para una proporción."""
    if not n:
        return None, None
    z = statistics.NormalDist().inv_cdf(1 - (1 - conf) / 2)
    p = k / n
    den = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / den
    mitad = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centro - mitad), min(1.0, centro + mitad)


def cuantil(valores, q):
    """Cuantil con interpolación lineal (igual que numpy.percentile por defecto)."""
    s = sorted(valores)
    if not s:
        return None
    h = (len(s) - 1) * q
    lo = int(math.floor(h))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (h - lo) * (s[hi] - s[lo])


def media(v):
    return sum(v) / len(v) if v else None


def desvio(v):
    return statistics.stdev(v) if len(v) > 1 else None


def rangos(valores):
    """Rangos con promedio en los empates; devuelve (rangos, suma de (t^3 - t) de los empates)."""
    n = len(valores)
    orden = sorted(range(n), key=lambda i: valores[i])
    r = [0.0] * n
    corr = 0.0
    i = 0
    while i < n:
        j = i
        while j + 1 < n and valores[orden[j + 1]] == valores[orden[i]]:
            j += 1
        prom = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[orden[k]] = prom
        t = j - i + 1
        corr += t ** 3 - t
        i = j + 1
    return r, corr


def mann_whitney(x, y):
    """Mann-Whitney U (aproximación normal con corrección por empates y por continuidad, como scipy 'asymptotic').
    Devuelve (U1, p, r) con r = correlación biserial por rangos (= delta de Cliff): >0 si x tiende a ser mayor que y."""
    n1, n2 = len(x), len(y)
    if n1 == 0 or n2 == 0:
        return None, None, None
    r, corr = rangos(list(x) + list(y))
    u1 = sum(r[:n1]) - n1 * (n1 + 1) / 2.0
    u2 = n1 * n2 - u1
    n = n1 + n2
    mu = n1 * n2 / 2.0
    var = n1 * n2 / 12.0 * ((n + 1) - corr / (n * (n - 1))) if n > 1 else 0.0
    if var <= 0:
        return u1, 1.0, 2 * u1 / (n1 * n2) - 1
    z = (max(u1, u2) - mu - 0.5) / math.sqrt(var)
    p = min(1.0, 2 * norm_sf(z))
    return u1, p, 2 * u1 / (n1 * n2) - 1


def kruskal(grupos):
    """Kruskal-Wallis con corrección por empates. Devuelve (H, p)."""
    grupos = [list(g) for g in grupos if len(g) > 0]
    if len(grupos) < 2:
        return None, None
    todos = [v for g in grupos for v in g]
    n = len(todos)
    r, corr = rangos(todos)
    pos, suma = 0, 0.0
    for g in grupos:
        rg = sum(r[pos:pos + len(g)])
        suma += rg * rg / len(g)
        pos += len(g)
    h = 12.0 / (n * (n + 1)) * suma - 3 * (n + 1)
    den = 1 - corr / (n ** 3 - n) if n > 1 else 1
    if den <= 0:
        return None, None
    h /= den
    return h, chi2_sf(h, len(grupos) - 1)


def holm(pvalores):
    """Corrección de Holm; None se respeta."""
    idx = [i for i, p in enumerate(pvalores) if p is not None]
    idx.sort(key=lambda i: pvalores[i])
    m = len(idx)
    ajust = [None] * len(pvalores)
    previo = 0.0
    for rango, i in enumerate(idx):
        previo = max(previo, min(1.0, (m - rango) * pvalores[i]))
        ajust[i] = previo
    return ajust


# ======================================================================================================================
# Regresión logística con errores robustos por conglomerado (participante)
# ======================================================================================================================
def _invertir(A):
    n = len(A)
    M = [list(map(float, A[i])) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            return None
        M[c], M[piv] = M[piv], M[c]
        d = M[c][c]
        M[c] = [v / d for v in M[c]]
        for r in range(n):
            if r != c and M[r][c] != 0.0:
                f = M[r][c]
                M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    return [fila[n:] for fila in M]


def _matvec(A, v):
    return [sum(a * b for a, b in zip(fila, v)) for fila in A]


def logit_robusto(X, y, cluster):
    """Regresión logística (IRLS) con errores estándar robustos por conglomerado (CR1, igual que statsmodels cov_type='cluster').
    Devuelve dict con beta, se, z, p; o {'ok': False, 'motivo': ...} si el modelo no es estimable (separación, colinealidad)."""
    n, k = len(y), len(X[0])
    if n < k + 2 or len(set(y)) < 2:
        return {'ok': False, 'motivo': 'datos insuficientes'}
    beta = [0.0] * k
    convergio = False
    A = None
    for _ in range(60):
        eta = [sum(a * b for a, b in zip(fila, beta)) for fila in X]
        p = [1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, e)))) for e in eta]
        w = [max(pi * (1 - pi), 1e-9) for pi in p]
        z = [e + (yi - pi) / wi for e, yi, pi, wi in zip(eta, y, p, w)]
        A = [[sum(w[i] * X[i][a] * X[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
        bvec = [sum(w[i] * X[i][a] * z[i] for i in range(n)) for a in range(k)]
        Ainv = _invertir(A)
        if Ainv is None:
            return {'ok': False, 'motivo': 'colinealidad'}
        nuevo = _matvec(Ainv, bvec)
        delta = max(abs(a - b) for a, b in zip(nuevo, beta))
        beta = nuevo
        if max(abs(b) for b in beta) > 14:
            return {'ok': False, 'motivo': 'separación (algún grupo tiene 0 % o 100 % de aciertos)'}
        if delta < 1e-9:
            convergio = True
            break
    if not convergio:
        return {'ok': False, 'motivo': 'no convergió (probable separación)'}
    eta = [sum(a * b for a, b in zip(fila, beta)) for fila in X]
    p = [1.0 / (1.0 + math.exp(-e)) for e in eta]
    w = [max(pi * (1 - pi), 1e-12) for pi in p]
    A = [[sum(w[i] * X[i][a] * X[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    Ainv = _invertir(A)
    if Ainv is None:
        return {'ok': False, 'motivo': 'colinealidad'}
    puntajes = {}
    for i in range(n):
        u = puntajes.setdefault(cluster[i], [0.0] * k)
        r = y[i] - p[i]
        for a in range(k):
            u[a] += X[i][a] * r
    G = len(puntajes)
    if G < 2:
        return {'ok': False, 'motivo': 'un solo participante'}
    B = [[sum(u[a] * u[b] for u in puntajes.values()) for b in range(k)] for a in range(k)]
    c = G / (G - 1.0) * (n - 1.0) / max(n - k, 1.0)
    AB = [[sum(Ainv[a][m] * B[m][b] for m in range(k)) for b in range(k)] for a in range(k)]
    V = [[c * sum(AB[a][m] * Ainv[m][b] for m in range(k)) for b in range(k)] for a in range(k)]
    se = [math.sqrt(max(V[a][a], 0.0)) for a in range(k)]
    zs = [beta[a] / se[a] if se[a] > 0 else None for a in range(k)]
    ps = [min(1.0, 2 * norm_sf(abs(zv))) if zv is not None else None for zv in zs]
    return {'ok': True, 'beta': beta, 'se': se, 'V': V, 'z': zs, 'p': ps, 'n': n, 'G': G}


def contraste_wald(modelo, L):
    """Contraste lineal L'beta: devuelve (estimación, ee, p)."""
    b, V = modelo['beta'], modelo['V']
    est = sum(l * x for l, x in zip(L, b))
    var = sum(L[i] * V[i][j] * L[j] for i in range(len(L)) for j in range(len(L)))
    if var <= 0:
        return est, None, None
    ee = math.sqrt(var)
    return est, ee, min(1.0, 2 * norm_sf(abs(est / ee)))


def razon_odds(est, ee):
    """(OR, inferior, superior) a partir de un coeficiente logit y su error estándar."""
    if est is None or ee is None:
        return None, None, None
    return math.exp(est), math.exp(est - Z95 * ee), math.exp(est + Z95 * ee)


# ======================================================================================================================
# Prueba de permutación estratificada (conglomerados = participantes)
# ======================================================================================================================
def permutacion(aciertos, totales, nivel, estrato, B, rng, hasta=None):
    """Compara la proporción de aciertos (pooled por espécimen) entre nivel 1 y nivel 0, permutando las etiquetas de nivel ENTRE
    PARTICIPANTES dentro de cada estrato (p. ej. el semestre). Cada participante conserva sus especímenes juntos, así se respeta
    el conglomerado. Devuelve dict(dif, p, B). Estable con grupos pequeños y con 0 % / 100 % de aciertos."""
    n = len(aciertos)
    s1 = sum(aciertos[i] for i in range(n) if nivel[i] == 1)
    n1 = sum(totales[i] for i in range(n) if nivel[i] == 1)
    s0 = sum(aciertos[i] for i in range(n) if nivel[i] == 0)
    n0 = sum(totales[i] for i in range(n) if nivel[i] == 0)
    if n1 == 0 or n0 == 0:
        return None
    obs = s1 / n1 - s0 / n0
    estratos = {}
    for i in range(n):
        estratos.setdefault(estrato[i], []).append(i)
    listas = [idx for idx in estratos.values() if len(idx) > 1 and len({nivel[i] for i in idx}) > 1]
    comparables = sum(len(idx) for idx in listas)
    if not listas:
        return {'dif': obs, 'p': None, 'B': 0, 'comparables': 0}
    etiquetas = {id(idx): [nivel[i] for i in idx] for idx in listas}
    nivel_perm = list(nivel)
    extremos, hechas = 0, 0
    for b in range(B):
        if hasta is not None and b % 100 == 0 and time.time() > hasta:
            break
        for idx in listas:
            et = etiquetas[id(idx)]
            rng.shuffle(et)
            for pos, i in enumerate(idx):
                nivel_perm[i] = et[pos]
        t1 = u1 = t0 = u0 = 0
        for i in range(n):
            if nivel_perm[i] == 1:
                t1 += aciertos[i]
                u1 += totales[i]
            else:
                t0 += aciertos[i]
                u0 += totales[i]
        if u1 and u0:
            if abs(t1 / u1 - t0 / u0) >= abs(obs) - 1e-12:
                extremos += 1
        else:
            extremos += 1
        hechas += 1
    if hechas < min(200, B):
        return {'dif': obs, 'p': None, 'B': hechas, 'truncado': True, 'comparables': comparables}          # sin tiempo suficiente: mejor no dar un p que dar uno falso
    return {'dif': obs, 'p': (1 + extremos) / (hechas + 1), 'B': hechas, 'truncado': hechas < B, 'comparables': comparables}


# ======================================================================================================================
# Otras utilidades
# ======================================================================================================================
def chi2_tabla(tabla):
    filas = [sum(f) for f in tabla]
    cols = [sum(c) for c in zip(*tabla)]
    total = sum(filas)
    if total == 0:
        return 0.0
    est = 0.0
    for i, f in enumerate(tabla):
        for j, o in enumerate(f):
            e = filas[i] * cols[j] / total
            if e > 0:
                est += (o - e) ** 2 / e
    return est


def icc_binario(aciertos, totales):
    """Correlación intraclase (estimador ANOVA) para un resultado binario agrupado por participante."""
    g = len(aciertos)
    n = sum(totales)
    if g < 3 or n <= g:
        return None
    pbar = sum(aciertos) / n
    ssb = sum((a / t - pbar) ** 2 * t for a, t in zip(aciertos, totales) if t)
    ssw = sum(a - a * a / t for a, t in zip(aciertos, totales) if t)
    msb, msw = ssb / (g - 1), ssw / (n - g)
    m0 = (n - sum(t * t for t in totales) / n) / (g - 1)
    den = msb + (m0 - 1) * msw
    return None if den <= 0 else max(0.0, min(0.9, (msb - msw) / den))


def dif_minima_detectable(p1, n_por_grupo, conf=0.95, potencia=0.80):
    """Menor p2 > p1 detectable (aproximación normal, dos colas) con n_por_grupo observaciones efectivas por grupo."""
    if n_por_grupo < 2 or p1 is None:
        return None
    za, zb = statistics.NormalDist().inv_cdf(1 - (1 - conf) / 2), statistics.NormalDist().inv_cdf(potencia)
    lo, hi = p1, 0.9999
    for _ in range(80):
        p2 = (lo + hi) / 2
        pb = (p1 + p2) / 2
        z = (abs(p2 - p1) * math.sqrt(n_por_grupo) - za * math.sqrt(2 * pb * (1 - pb))) / math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
        if z >= zb:
            hi = p2
        else:
            lo = p2
    return hi


# ======================================================================================================================
# Preparación de los datos (a partir de las tablas del paquete de análisis)
# ======================================================================================================================
def _filas(t):
    cols = t['columnas']
    return [dict(zip(cols, f)) for f in t['filas']]


def _num(v):
    if v in (None, ''):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def preparar(tablas, incluir_repetidos=False):
    esp = _filas(tablas['especimenes'])
    par = {f['id_participante']: f for f in _filas(tablas['participantes'])}
    estudiantes = [p for p in par.values() if p['rol'] == 'estudiante']
    obs, repetidos_excl, anulados_excl = [], 0, 0
    for r in esp:
        if r['rol'] != 'estudiante' or r['acierto'] not in (0, 1):
            continue
        if r.get('sesion_anulada') == 1:                       # un intento anulado se conserva en los datos, pero no entra al análisis
            anulados_excl += 1
            continue
        p = par.get(r['id_participante'])
        if not p or p['grupo'] not in GRUPOS:
            continue
        if not incluir_repetidos and r['es_primer_intento_especimen'] != 1:
            repetidos_excl += 1
            continue
        obs.append(r)
    personas = {}
    for r in obs:
        p = par[r['id_participante']]
        d = personas.setdefault(r['id_participante'], {'id': r['id_participante'], 'grupo': p['grupo'], 'semestre': _num(p['semestre']), 'k': 0, 'n': 0, 'tiempos': [],
                                                       'sus': _num(p['sus_total']), 'ccp_i': _num(p['ccp_intrinseca']), 'ccp_e': _num(p['ccp_extrinseca']), 'ccp_g': _num(p['ccp_germana'])})
        d['k'] += int(r['acierto'])
        d['n'] += 1
        t = _num(r['tiempo_clave_s'])
        if t is not None:
            d['tiempos'].append(t)
    for d in personas.values():
        d['tiempo_medio'] = media(d['tiempos'])
        d['R'] = 1 if d['grupo'].startswith('3D') else 0
        d['M'] = 1 if 'META' in d['grupo'] else 0
    return {'obs': obs, 'personas': sorted(personas.values(), key=lambda d: d['id']), 'estudiantes': estudiantes, 'par': par, 'repetidos_excl': repetidos_excl, 'anulados_excl': anulados_excl, 'esp_todas': esp}


# ======================================================================================================================
# Análisis completo
# ======================================================================================================================
def _r(v, d=2):
    return '' if v is None else round(v, d)


def _pct(v, d=1):
    return '' if v is None else round(100 * v, d)


def _p(v):
    return '' if v is None else (round(v, 4) if v >= 0.0001 else '<0.0001')


def analizar(tablas, B=3000, semilla=20261110, incluir_repetidos=False, presupuesto_s=12.0):
    t_ini = time.time()
    rng = random.Random(semilla)
    D = preparar(tablas, incluir_repetidos)
    per, obs = D['personas'], D['obs']
    T = {}
    avisos, lectura = [], []
    if not per:
        return {'ok': True, 'vacio': True, 'mensaje': 'Todavía no hay estudiantes con grupo y al menos un espécimen identificado.', 'tablas': {}, 'lectura': [], 'avisos': []}

    # ---------------------------------------------------------------- muestra
    por_g = {g: [d for d in per if d['grupo'] == g] for g in GRUPOS}
    filas = [[g, len(por_g[g]), sum(d['n'] for d in por_g[g]), sum(1 for d in por_g[g] if d['n'] >= 2), sum(1 for d in por_g[g] if d['n'] == 1)] for g in GRUPOS]
    filas.append(['Total', len(per), sum(d['n'] for d in per), sum(1 for d in per if d['n'] >= 2), sum(1 for d in per if d['n'] == 1)])
    T['muestra'] = {'titulo': 'Muestra analizada', 'columnas': ['Grupo', 'Personas', 'Especímenes', 'Personas con 2 especímenes', 'Personas con 1 espécimen'], 'filas': filas,
                    'nota': 'Solo estudiantes con grupo asignado. ' + ('Se cuentan todos los intentos.' if incluir_repetidos else 'Se usa el primer intento de cada espécimen (se descartan los repetidos por un reinicio).')}
    semestres = sorted({d['semestre'] for d in per if d['semestre'] is not None})
    tabla_sg = [[sum(1 for d in por_g[g] if d['semestre'] == s) for g in GRUPOS] for s in semestres]
    filas_sg = [[int(s)] + tabla_sg[i] + [sum(tabla_sg[i])] for i, s in enumerate(semestres)]
    p_balance = None
    if len(semestres) >= 2 and sum(1 for g in GRUPOS if por_g[g]) >= 2:
        obs_chi = chi2_tabla(tabla_sg)
        etiquetas = [d['grupo'] for d in per if d['semestre'] is not None]
        sems = [d['semestre'] for d in per if d['semestre'] is not None]
        ext, hechas = 0, 0
        for _ in range(2000):
            rng.shuffle(etiquetas)
            tb = [[0] * 4 for _ in semestres]
            for s, g in zip(sems, etiquetas):
                tb[semestres.index(s)][GRUPOS.index(g)] += 1
            if chi2_tabla(tb) >= obs_chi - 1e-9:
                ext += 1
            hechas += 1
        p_balance = (1 + ext) / (hechas + 1)
    T['grupo_por_semestre'] = {'titulo': 'Personas por grupo y semestre', 'columnas': ['Semestre'] + GRUPOS + ['Total'], 'filas': filas_sg,
                               'nota': 'Si el grupo depende del semestre, una diferencia entre grupos puede ser en realidad una diferencia entre semestres.' + ('' if p_balance is None else f' Prueba de independencia grupo × semestre (permutación): p = {_p(p_balance)}.')}
    if p_balance is not None and p_balance < 0.10:
        avisos.append(f'El grupo y el semestre NO están repartidos al azar (p = {_p(p_balance)}): las comparaciones «sin ajustar» pueden reflejar diferencias de semestre. Usa las filas «Ajustado por semestre».')
    chicos = [g for g in GRUPOS if len(por_g[g]) < 10]
    if chicos:
        avisos.append('Grupos con menos de 10 personas: ' + ', '.join(f'{g} ({len(por_g[g])})' for g in chicos) + '. Sus estimaciones son muy imprecisas.')

    # ---------------------------------------------------------------- descriptivos
    niveles = [('2D', lambda d: d['grupo'] == '2D'), ('2D_META', lambda d: d['grupo'] == '2D_META'), ('3D', lambda d: d['grupo'] == '3D'), ('3D_META', lambda d: d['grupo'] == '3D_META'),
               ('Todos los 2D', lambda d: d['R'] == 0), ('Todos los 3D', lambda d: d['R'] == 1), ('Sin metacognición', lambda d: d['M'] == 0), ('Con metacognición', lambda d: d['M'] == 1), ('Total', lambda d: True)]
    filas = []
    tiempos_esp = {}
    for nombre, f in niveles:
        sel = [d for d in per if f(d)]
        ids = {d['id'] for d in sel}
        k, n = sum(d['k'] for d in sel), sum(d['n'] for d in sel)
        lo, hi = wilson(k, n)
        tms = [_num(r['tiempo_clave_s']) for r in obs if r['id_participante'] in ids and _num(r['tiempo_clave_s']) is not None]
        filas.append([nombre, len(sel), n, k, _pct(k / n if n else None), _pct(lo), _pct(hi), _r(cuantil(tms, .5), 0), _r(cuantil(tms, .25), 0), _r(cuantil(tms, .75), 0)])
    T['descriptivos'] = {'titulo': 'Acierto y tiempo por grupo', 'columnas': ['Nivel', 'Personas', 'Especímenes', 'Aciertos', '% acierto', 'IC95 inf', 'IC95 sup', 'Tiempo mediano (s)', 'Q1 (s)', 'Q3 (s)'], 'filas': filas,
                         'nota': 'IC de Wilson calculado por espécimen (ignora que cada persona aporta 2, así que es algo más estrecho de lo real). Azar de referencia ≈ 11 % (1 de 9 especies).'}
    filas = []
    for s in semestres:
        sel = [d for d in per if d['semestre'] == s]
        k, n = sum(d['k'] for d in sel), sum(d['n'] for d in sel)
        lo, hi = wilson(k, n)
        filas.append([int(s), len(sel), n, k, _pct(k / n if n else None), _pct(lo), _pct(hi)])
    T['por_semestre'] = {'titulo': 'Acierto por semestre', 'columnas': ['Semestre', 'Personas', 'Especímenes', 'Aciertos', '% acierto', 'IC95 inf', 'IC95 sup'], 'filas': filas, 'nota': 'Para ver cuánto del resultado depende del semestre.'}

    # ---------------------------------------------------------------- modelos y permutaciones (efectos principales)
    por_prueba = presupuesto_s / 10.0              # 4 contrastes + 6 pares: cada prueba tiene su propio tiempo máximo
    truncadas = [False]
    def plazo():
        return time.time() + por_prueba

    def perm(*a):
        r = permutacion(*a)
        if r and r.get('truncado'):
            truncadas[0] = True
        return r

    B_ef = max(500, B)
    ac = [d['k'] for d in per]
    tot = [d['n'] for d in per]
    Rv, Mv = [d['R'] for d in per], [d['M'] for d in per]
    sem_k = [d['semestre'] if d['semestre'] is not None else 'sin' for d in per]

    def ajustar_modelo(con_semestre, inter=True):
        X, y, cl = [], [], []
        sub = [d for d in per if (d['semestre'] is not None or not con_semestre)]
        if not sub:
            return None, 0
        mu = media([d['semestre'] for d in sub if d['semestre'] is not None]) if con_semestre else 0
        for d in sub:
            r_, m_ = d['R'] - .5, d['M'] - .5
            fila = [1.0, r_, m_] + ([r_ * m_] if inter else []) + ([d['semestre'] - mu] if con_semestre else [])
            ya = d['k']
            for j in range(d['n']):
                X.append(fila)
                y.append(1 if j < ya else 0)
                cl.append(d['id'])
        return logit_robusto(X, y, cl), len(sub)

    modelos = {}
    for etiqueta, con_sem in (('Sin ajustar', False), ('Ajustado por semestre', True)):
        completo = ajustar_modelo(con_sem, True)
        if completo[0] and completo[0].get('ok'):
            modelos[etiqueta] = (completo[0], completo[1], 'completo')
        else:                                                                   # sin interacción el modelo suele poder estimarse aunque una celda esté vacía
            adit = ajustar_modelo(con_sem, False)
            modelos[etiqueta] = (adit[0], adit[1], 'aditivo') if adit[0] and adit[0].get('ok') else (completo[0], completo[1], 'ninguno')
    filas, sep_aviso, info = [], False, {}
    n_g = {g: len(por_g[g]) for g in GRUPOS}
    lados = {1: (sum(1 for d in per if d['R'] == 1), sum(1 for d in per if d['R'] == 0)), 2: (sum(1 for d in per if d['M'] == 1), sum(1 for d in per if d['M'] == 0))}
    for ajuste, (mod, n_pers, tipo_mod) in modelos.items():
        for nombre, col, r_est in [('3D frente a 2D', 1, lambda d: d['R']), ('Con frente a sin metacognición', 2, lambda d: d['M']), ('Interacción (3D × metacognición)', 3, None)]:
            comp, p_perm = None, None
            if r_est is not None:
                niv = Rv if col == 1 else Mv
                otro = Mv if col == 1 else Rv
                est = [(otro[i], sem_k[i]) for i in range(len(per))] if ajuste != 'Sin ajustar' else [otro[i] for i in range(len(per))]
                pr = perm(ac, tot, niv, est, B_ef, rng, plazo())
                dif = pr['dif'] * 100 if pr else None
                p_perm = pr['p'] if pr else None
                comp = pr.get('comparables') if pr else None
                lado = f'{lados[col][0]} frente a {lados[col][1]}'
                aviso = ''
                if min(lados[col]) < 5:
                    aviso = 'Muy pocas personas en un lado: no interpretar.'
                elif min(lados[col]) < 10:
                    aviso = 'Pocas personas en un lado: solo orientativo.'
                if ajuste != 'Sin ajustar' and comp is not None and comp < 10:
                    aviso = (aviso + ' ' if aviso else '') + 'Casi no hay personas del mismo semestre en ambos niveles: la comparación ajustada casi no tiene información.'
            else:
                def acc(g):
                    k_, n_ = sum(d['k'] for d in por_g[g]), sum(d['n'] for d in por_g[g])
                    return k_ / n_ if n_ else None
                a = [acc(g) for g in GRUPOS]
                chico = min(n_g.values()) < 3
                dif = ((a[3] - a[2]) - (a[1] - a[0])) * 100 if (all(v is not None for v in a) and not chico) else None
                lado, aviso = '', ('Algún grupo tiene menos de 3 personas: no se calcula.' if chico else '')
            if mod and mod.get('ok') and not (tipo_mod == 'aditivo' and col == 3):
                orr, lo, hi = razon_odds(mod['beta'][col], mod['se'][col])
                p_mod = mod['p'][col]
                if tipo_mod == 'aditivo' and col != 3:
                    aviso = (aviso + ' ' if aviso else '') + 'OR y p del modelo SIN interacción (el modelo completo no se puede estimar).'
            else:
                orr = lo = hi = p_mod = None
                if tipo_mod == 'ninguno' or (tipo_mod == 'aditivo' and col == 3):
                    sep_aviso = sep_aviso or tipo_mod == 'ninguno'
            info[(ajuste, nombre)] = {'dif': dif, 'p_perm': p_perm, 'p_mod': p_mod, 'comp': comp, 'min_lado': min(lados[col]) if r_est is not None else None, 'fragil': bool(aviso)}
            filas.append([ajuste, nombre, _r(dif, 1), _r(orr, 2), _r(lo, 2), _r(hi, 2), _p(p_mod), _p(p_perm) if r_est is not None else 'no aplica', lado, '' if comp is None else comp, aviso])
    T['contrastes'] = {'titulo': 'Efectos principales e interacción', 'columnas': ['Ajuste', 'Efecto', 'Diferencia (puntos %)', 'OR', 'IC95 inf', 'IC95 sup', 'p (modelo logístico)', 'p (permutación)', 'Personas por lado', 'Personas que aportan (mismo semestre)', 'Aviso'], 'filas': filas,
                       'nota': 'Diferencia en puntos porcentuales de acierto (nivel indicado menos el de referencia). OR > 1 = más aciertos. «p (permutación)» compara solo personas del mismo semestre (en las filas «Ajustado por semestre») y respeta que cada persona aporta 2 especímenes; el modelo logístico usa errores robustos por participante. «Personas que aportan» = quienes tienen compañeros del otro nivel en su mismo semestre: si es muy bajo, esa comparación casi no tiene información. La interacción solo tiene p del modelo.'}
    if sep_aviso:
        avisos.append('El modelo logístico no se pudo estimar ni siquiera sin interacción (algún nivel tiene 0 % o 100 % de aciertos, o hay muy pocos datos): se muestran la diferencia y la prueba de permutación, que sí son válidas.')

    # ---------------------------------------------------------------- comparaciones por pares
    pares = [(GRUPOS[i], GRUPOS[j]) for i in range(4) for j in range(i + 1, 4)]
    X, y, cl = [], [], []
    for d in per:
        fila = [1.0] + [1.0 if d['grupo'] == g else 0.0 for g in GRUPOS[1:]]
        for j in range(d['n']):
            X.append(fila)
            y.append(1 if j < d['k'] else 0)
            cl.append(d['id'])
    mod4 = logit_robusto(X, y, cl) if len({d['grupo'] for d in per}) == 4 else {'ok': False}
    filas_p, p_list = [], []
    for g1, g2 in pares:
        sel = [d for d in per if d['grupo'] in (g1, g2)]
        if not por_g[g1] or not por_g[g2]:
            filas_p.append([f'{g2} frente a {g1}', '', '', '', '', '', '', len(por_g[g1]), len(por_g[g2]), 'Un grupo no tiene participantes.'])
            p_list.append(None)
            continue
        pr = perm([d['k'] for d in sel], [d['n'] for d in sel], [1 if d['grupo'] == g2 else 0 for d in sel], [d['semestre'] if d['semestre'] is not None else 'sin' for d in sel], max(500, B // 2), rng, plazo())
        orr = lo = hi = None
        if mod4.get('ok'):
            L = [0.0] * 4
            if g2 != '2D':
                L[GRUPOS.index(g2)] += 1
            if g1 != '2D':
                L[GRUPOS.index(g1)] -= 1
            est, ee, _ = contraste_wald(mod4, L)
            orr, lo, hi = razon_odds(est, ee)
        p_list.append(pr['p'] if pr else None)
        mn = min(len(por_g[g1]), len(por_g[g2]))
        av = 'Muy pocas personas: no interpretar.' if mn < 5 else ('Pocas personas: solo orientativo.' if mn < 10 else '')
        if pr is not None and pr['p'] is None and not pr.get('truncado'):
            av = (av + ' ' if av else '') + 'Sin compañeros del otro grupo en el mismo semestre: no se puede comparar.'
        filas_p.append([f'{g2} frente a {g1}', _r(pr['dif'] * 100 if pr else None, 1), _r(orr, 2), _r(lo, 2), _r(hi, 2), _p(pr['p'] if pr else None), None, len(por_g[g1]), len(por_g[g2]), av])
    for fila, ph in zip(filas_p, holm(p_list)):
        fila[6] = _p(ph)
    T['pares'] = {'titulo': 'Comparaciones entre los 4 grupos (por pares)', 'columnas': ['Comparación', 'Diferencia (puntos %)', 'OR', 'IC95 inf', 'IC95 sup', 'p (permutación, por semestre)', 'p ajustado (Holm)', 'Personas grupo base', 'Personas grupo comparado', 'Aviso'], 'filas': filas_p,
                  'nota': 'Seis comparaciones: se corrige por comparaciones múltiples con el método de Holm. La permutación compara solo personas del mismo semestre. El OR solo aparece si el modelo se puede estimar.'}

    if truncadas[0]:
        avisos.append('Por el límite de tiempo del servidor se hicieron menos permutaciones de las pedidas (o no alcanzaron para dar un p en alguna prueba). Vuelve a calcular o reduce el número de permutaciones.')

    # ---------------------------------------------------------------- tiempos y encuestas (nivel persona, pruebas de rangos)
    variables = [('Tiempo medio por espécimen (s)', 'tiempo_medio'), ('SUS (0–100)', 'sus'), ('Carga intrínseca (0–40)', 'ccp_i'), ('Carga extrínseca (0–40)', 'ccp_e'), ('Carga germana (0–20)', 'ccp_g')]
    filas_d, filas_t = [], []
    for nombre, clave in variables:
        grupos_v = {g: [d[clave] for d in por_g[g] if d[clave] is not None] for g in GRUPOS}
        for g in GRUPOS:
            v = grupos_v[g]
            filas_d.append([nombre, g, len(v), _r(cuantil(v, .5), 1), _r(cuantil(v, .25), 1), _r(cuantil(v, .75), 1), _r(media(v), 1), _r(desvio(v), 1)])
        h, pk = kruskal(list(grupos_v.values()))
        filas_t.append([nombre, 'Los 4 grupos (Kruskal-Wallis)', _p(pk), ''])
        v3, v2 = grupos_v['3D'] + grupos_v['3D_META'], grupos_v['2D'] + grupos_v['2D_META']
        vm, vs = grupos_v['2D_META'] + grupos_v['3D_META'], grupos_v['2D'] + grupos_v['3D']
        for etq, a_, b_ in [('3D frente a 2D', v3, v2), ('Con frente a sin metacognición', vm, vs)]:
            u, pm, rb = mann_whitney(a_, b_)
            filas_t.append([nombre, etq + ' (Mann-Whitney)', _p(pm), _r(rb, 2)])
    T['tiempos_encuestas'] = {'titulo': 'Tiempo, usabilidad (SUS) y carga cognitiva por grupo', 'columnas': ['Variable', 'Grupo', 'n', 'Mediana', 'Q1', 'Q3', 'Media', 'DE'], 'filas': filas_d, 'nota': 'Un valor por persona.'}
    T['pruebas_encuestas'] = {'titulo': 'Pruebas para tiempo, SUS y carga cognitiva', 'columnas': ['Variable', 'Comparación', 'p', 'Tamaño del efecto r (biserial por rangos)'], 'filas': filas_t,
                              'nota': 'r va de −1 a 1 (positivo = el primer nivel tiende a valores mayores). Pruebas de rangos: no suponen distribución normal. No se ajustan por semestre.'}

    # ---------------------------------------------------------------- especies
    esp_filas, conf = [], {}
    for r in obs:
        conf.setdefault(r['especie_correcta'], {'n': 0, 'k': 0, 'err': {}})
        c = conf[r['especie_correcta']]
        c['n'] += 1
        c['k'] += int(r['acierto'])
        if not r['acierto'] and r['especie_seleccionada']:
            c['err'][r['especie_seleccionada']] = c['err'].get(r['especie_seleccionada'], 0) + 1
    for e in sorted(conf):
        c = conf[e]
        lo, hi = wilson(c['k'], c['n'])
        top = max(c['err'].items(), key=lambda kv: kv[1]) if c['err'] else ('', 0)
        esp_filas.append([e, c['n'], c['k'], _pct(c['k'] / c['n']), _pct(lo), _pct(hi), top[0], top[1]])
    T['especies'] = {'titulo': 'Acierto por especie', 'columnas': ['Especie', 'Presentaciones', 'Aciertos', '% acierto', 'IC95 inf', 'IC95 sup', 'Error más frecuente (especie elegida)', 'Veces'], 'filas': esp_filas,
                     'nota': 'La dificultad varía mucho entre especies: si un grupo recibe más especies difíciles, parece que rinde menos. Revisa la tabla siguiente.'}
    reparto = {}
    for r in obs:
        reparto.setdefault(r['especie_correcta'], {g: 0 for g in GRUPOS})[r['grupo']] += 1
    T['especies_por_grupo'] = {'titulo': 'Presentaciones de cada especie por grupo', 'columnas': ['Especie'] + GRUPOS, 'filas': [[e] + [reparto[e][g] for g in GRUPOS] for e in sorted(reparto)], 'nota': 'Debería estar equilibrada dentro de cada grupo.'}

    # ---------------------------------------------------------------- calibración metacognitiva
    filas = []
    for etq, grs in [('2D_META', ['2D_META']), ('3D_META', ['3D_META']), ('Ambos grupos con metacognición', ['2D_META', '3D_META'])]:
        sel = [(r['id_participante'], _num(r['post_acierto_percibido']), 100.0 * r['acierto']) for r in obs if r['grupo'] in grs and _num(r['post_acierto_percibido']) is not None]
        if not sel:
            filas.append([etq, 0, 0, '', '', '', '', '', ''])
            continue
        dif = [p_ - real for _, p_, real in sel]
        porp = {}
        for (i, p_, real), dv in zip(sel, dif):
            porp.setdefault(i, []).append(dv)
        ids = list(porp)
        medias_b = []
        for _ in range(1500):
            muestra = [rng.choice(ids) for _ in ids]
            vs = [v for i in muestra for v in porp[i]]
            medias_b.append(sum(vs) / len(vs))
        medias_b.sort()
        filas.append([etq, len(sel), len(ids), _r(media([s[1] for s in sel]), 1), _r(media([s[2] for s in sel]), 1), _r(media(dif), 1), _r(medias_b[int(0.025 * len(medias_b))], 1), _r(medias_b[int(0.975 * len(medias_b)) - 1], 1), _pct(sum(1 for v in dif if v > 0) / len(dif), 0)])
    T['calibracion'] = {'titulo': 'Calibración metacognitiva (grupos con metacognición)', 'columnas': ['Grupo', 'Especímenes', 'Personas', 'Acierto percibido medio (%)', 'Acierto real medio (%)', 'Diferencia media (puntos)', 'IC95 inf', 'IC95 sup', '% que se sobreestimó'], 'filas': filas,
                        'nota': 'Diferencia > 0 = la persona creyó haber acertado más de lo que acertó (sobreestimación). IC por bootstrap sobre participantes.'}

    # ---------------------------------------------------------------- potencia
    icc = icc_binario(ac, tot)
    icc_u = 0.3 if icc is None else icc
    deff = 1 + (media(tot) - 1) * icc_u if tot else 1
    p0 = sum(d['k'] for d in per if d['R'] == 0 and d['M'] == 0) / max(1, sum(d['n'] for d in per if d['R'] == 0 and d['M'] == 0)) if any(d['R'] == 0 and d['M'] == 0 for d in per) else sum(ac) / max(1, sum(tot))
    p0 = min(max(p0, 0.05), 0.9)
    filas = []
    for etq, f1, f0 in [('3D frente a 2D', lambda d: d['R'] == 1, lambda d: d['R'] == 0), ('Con frente a sin metacognición', lambda d: d['M'] == 1, lambda d: d['M'] == 0)] + [(f'{g2} frente a {g1}', (lambda g: lambda d: d['grupo'] == g)(g2), (lambda g: lambda d: d['grupo'] == g)(g1)) for g1, g2 in pares]:
        a_, b_ = [d for d in per if f1(d)], [d for d in per if f0(d)]
        n_ef = min(sum(d['n'] for d in a_), sum(d['n'] for d in b_)) / deff
        pmin = dif_minima_detectable(p0, n_ef)
        filas.append([etq, len(a_), len(b_), _r(n_ef, 0), _pct(p0, 0), '' if pmin is None else _r(100 * (pmin - p0), 0), '' if pmin is None else _pct(pmin, 0)])
    T['potencia'] = {'titulo': 'Diferencia mínima detectable con la muestra actual', 'columnas': ['Comparación', 'Personas (comparado)', 'Personas (base)', 'Especímenes efectivos por lado', '% acierto de partida', 'Diferencia mínima (puntos)', '% acierto mínimo detectable'], 'filas': filas,
                     'nota': f'Aproximación normal, 80 % de potencia, α = 0,05. Efecto de conglomerado: correlación intrapersona ≈ {icc_u:.2f} ({"estimada" if icc is not None else "supuesta"}). Una diferencia menor a esta puede existir y no ser detectable todavía.'}

    # ---------------------------------------------------------------- calidad
    est_ids = {p['id_participante'] for p in D['estudiantes']}
    en_analisis = {d['id'] for d in per}
    filas = [['Estudiantes registrados', len(est_ids)], ['Estudiantes con grupo asignado', sum(1 for p in D['estudiantes'] if p['grupo'] in GRUPOS)], ['Estudiantes con al menos un espécimen analizado', len(per)],
             ['Estudiantes sin ningún espécimen (no entran al análisis)', len(est_ids) - len(en_analisis)], ['Especímenes analizados', len(obs)], ['Especímenes repetidos descartados (reinicios)', 0 if incluir_repetidos else D['repetidos_excl']],
             ['Especímenes de intentos ANULADOS (conservados, excluidos del análisis)', D['anulados_excl']], ['Estudiantes con algún intento anulado', sum(1 for p in D['estudiantes'] if (p.get('n_sesiones_anuladas') or 0) > 0)],
             ['Especímenes de intentos vigentes tras una anulación (vieron otras especies antes)', sum(1 for r in obs if r.get('reintento_tras_anulacion') == 1)],
             ['Especímenes con menos de 15 s (posible azar)', sum(1 for r in obs if r.get('tiempo_corto') == 1)],
             ['Reflexiones «post» con los tres deslizadores en 50', sum(1 for r in obs if r.get('post_sin_mover') == 1)], ['Personas sin puntaje SUS', sum(1 for d in per if d['sus'] is None)],
             ['Personas sin carga cognitiva', sum(1 for d in per if d['ccp_i'] is None)], ['Personas sin semestre', sum(1 for d in per if d['semestre'] is None)]]
    T['calidad'] = {'titulo': 'Calidad de los datos', 'columnas': ['Indicador', 'Valor'], 'filas': filas, 'nota': 'Los especímenes con menos de 15 s no se descartan automáticamente: revísalos.'}

    # ---------------------------------------------------------------- lectura prudente
    n_per, n_esp = len(per), len(obs)
    lectura.append(f'Se analizaron {n_per} estudiantes y {n_esp} especímenes ({"todos los intentos" if incluir_repetidos else "primer intento de cada espécimen"}). Acierto global: {_pct(sum(ac) / sum(tot))} % (azar ≈ 11 %).')
    for nombre in ('3D frente a 2D', 'Con frente a sin metacognición'):
        i = info.get(('Ajustado por semestre', nombre))
        if not i:
            continue
        dif, pp = i['dif'], i['p_perm']
        txt_p = '' if pp is None else f', p = {_p(pp)}'
        if i['fragil']:
            lectura.append(f'{nombre} (ajustado por semestre): diferencia de {_r(dif, 1)} puntos{txt_p}. FRÁGIL: ' + ('hay muy pocas personas en un lado ({} contra {})'.format(*(lados[1] if nombre.startswith('3D') else lados[2])) if i['min_lado'] is not None and i['min_lado'] < 10 else '') +
                           ('; ' if i['min_lado'] is not None and i['min_lado'] < 10 and i['comp'] is not None and i['comp'] < 10 else '') + (f"solo {i['comp']} personas tienen compañeros del otro nivel en su mismo semestre" if i['comp'] is not None and i['comp'] < 10 else '') + '. No lo interpretes todavía.')
        elif pp is None:
            lectura.append(f'{nombre} (ajustado por semestre): diferencia de {_r(dif, 1)} puntos; no se pudo calcular la prueba.')
        elif pp < 0.05:
            lectura.append(f'{nombre} (ajustado por semestre): diferencia de {_r(dif, 1)} puntos{txt_p}. Hay evidencia de una diferencia con esta muestra.')
        else:
            lectura.append(f'{nombre} (ajustado por semestre): diferencia de {_r(dif, 1)} puntos{txt_p}. NO es concluyente con esta muestra: ni confirma ni descarta un efecto (ver «Diferencia mínima detectable»).')
    avisos.append('Los primeros participantes se asignaron por curso completo, no al azar. Los análisis «ajustados por semestre» reducen ese problema pero no lo eliminan.')
    return {'ok': True, 'vacio': False, 'tablas': T, 'lectura': lectura, 'avisos': avisos, 'parametros': {'permutaciones': B, 'semilla': semilla, 'incluir_repetidos': incluir_repetidos, 'recortado_por_tiempo': truncadas[0], 'segundos': round(time.time() - t_ini, 1)}}


# ======================================================================================================================
# Script de R que reproduce los modelos principales (modelos mixtos) a partir del paquete de análisis
# ======================================================================================================================
SCRIPT_R = r'''# analisis.R — modelos principales del estudio Linepithema
# Se ejecuta con los CSV de este mismo paquete (colócalo en la misma carpeta que especimenes.csv y participantes.csv).
# Requiere R y los paquetes:  install.packages(c("lme4", "readr", "dplyr"))
suppressPackageStartupMessages({ library(readr); library(dplyr); library(lme4) })

esp <- read_csv("especimenes.csv", show_col_types = FALSE)
par <- read_csv("participantes.csv", show_col_types = FALSE)

# --- Datos del análisis principal: estudiantes con grupo, primer intento de cada espécimen ---
d <- esp %>%
  filter(rol == "estudiante", !is.na(grupo), grupo != "", es_primer_intento_especimen == 1, !is.na(acierto)) %>%
  mutate(
    R    = ifelse(grepl("^3D", grupo), 0.5, -0.5),        # 3D frente a 2D (codificación de efectos: el coeficiente es el efecto principal)
    M    = ifelse(grepl("META", grupo), 0.5, -0.5),       # con frente a sin metacognición
    sem_c = semestre - mean(semestre, na.rm = TRUE),      # semestre centrado
    id   = factor(id_participante),
    especie = factor(especie_correcta)
  )
cat("\nEstudiantes:", n_distinct(d$id), " | Especímenes:", nrow(d), " | Acierto global:", round(100 * mean(d$acierto), 1), "%\n")
print(d %>% group_by(grupo) %>% summarise(personas = n_distinct(id), especimenes = n(), pct_acierto = round(100 * mean(acierto), 1), .groups = "drop"))

# --- Modelos logísticos mixtos: intercepto aleatorio por persona (y por especie en el modelo 3) ---
# Cada modelo está protegido: si no se puede ajustar (típico cuando un grupo tiene 0 % o 100 % de aciertos, o hay muy pocas
# personas en un grupo) el script lo dice y CONTINÚA con el resto, en vez de detenerse.
ajustar <- function(formula, datos, nombre, familia = TRUE) {
  tryCatch(
    withCallingHandlers(
      if (familia) glmer(formula, data = datos, family = binomial, control = glmerControl(optimizer = "bobyqa", optCtrl = list(maxfun = 2e5)))
      else lmer(formula, data = datos),
      warning = function(w) { cat("  Aviso al ajustar", nombre, ":", conditionMessage(w), "\n"); invokeRestart("muffleWarning") }),
    error = function(e) {
      cat("  No se pudo ajustar", nombre, "-", conditionMessage(e), "\n",
          " (suele pasar si algún grupo tiene 0 % o 100 % de aciertos o muy pocas personas; el análisis del panel usa pruebas de permutación que sí lo manejan)\n")
      NULL })
}
razones <- function(m) {
  co <- summary(m)$coefficients
  data.frame(termino = rownames(co), OR = exp(co[, "Estimate"]), IC95_inf = exp(co[, "Estimate"] - 1.96 * co[, "Std. Error"]),
             IC95_sup = exp(co[, "Estimate"] + 1.96 * co[, "Std. Error"]), p = co[, "Pr(>|z|)"], row.names = NULL)
}
# Un modelo con coeficientes o errores estándar enormes NO es fiable (separación: algún grupo con 0 % o 100 % de aciertos): no se muestra.
estable <- function(m) { co <- summary(m)$coefficients; all(is.finite(co)) && max(abs(co[, "Estimate"])) < 10 && max(co[, "Std. Error"]) < 10 }
mostrar <- function(m, titulo) {
  cat("\n==", titulo, "==\n")
  if (is.null(m)) { cat("  (sin resultado)\n"); return(invisible(NULL)) }
  if (!estable(m)) {
    cat("  El modelo se ajustó, pero sus coeficientes son INESTABLES (separación: algún grupo tiene 0 % o 100 % de aciertos, o hay muy pocas personas).\n",
        " No interpretes estos números. Usa el análisis del panel (permutación estratificada por semestre), que sí maneja este caso.\n")
    return(invisible(NULL))
  }
  print(razones(m), digits = 3)
}
m1 <- ajustar(acierto ~ R * M + (1 | id), d, "el modelo 1")
m2 <- ajustar(acierto ~ R * M + sem_c + (1 | id), d, "el modelo 2")
m3 <- ajustar(acierto ~ R * M + sem_c + (1 | id) + (1 | especie), d, "el modelo 3")
mostrar(m1, "Modelo 1: sin ajustar")
mostrar(m2, "Modelo 2: ajustado por semestre")
mostrar(m3, "Modelo 3: + efecto aleatorio de la especie")
ms <- Filter(function(m) !is.null(m) && estable(m), list(`modelo 1` = m1, `modelo 2` = m2, `modelo 3` = m3))
if (length(ms) > 1) { cat("\nComparación de modelos (AIC, menor = mejor):\n"); print(data.frame(modelo = names(ms), AIC = round(sapply(ms, AIC), 1), row.names = NULL)) }

# --- Tiempo en la clave (escala logarítmica) ---
d_t <- d %>% filter(!is.na(tiempo_clave_s), tiempo_clave_s > 0)
t1 <- ajustar(log(tiempo_clave_s) ~ R * M + sem_c + (1 | id), d_t, "el modelo de tiempo", familia = FALSE)
cat("\n== Tiempo en la clave (log), modelo mixto ==\n")
if (is.null(t1)) cat("  (sin resultado)\n") else print(summary(t1)$coefficients, digits = 3)

# --- SUS y carga cognitiva (un valor por persona) ---
p <- par %>% filter(rol == "estudiante", grupo != "") %>% mutate(grupo = factor(grupo))
for (v in c("sus_total", "ccp_intrinseca", "ccp_extrinseca", "ccp_germana")) {
  x <- p[[v]]; ok <- !is.na(x)
  if (sum(ok) > 5) { cat("\nKruskal-Wallis de", v, "entre grupos: p =", signif(kruskal.test(x[ok] ~ p$grupo[ok])$p.value, 3), "\n") }
}
'''
