#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline_final.py — Vocabulario legislativo (HCDN 2006-2025)
============================================================
Un solo script. Entra el corpus de la etapa 2, salen las tablas finales.

    !python pipeline_final.py --base "/content/drive/MyDrive/6. Investigación/Investigación 2026/Vocabulario legislativo"

ENTRADAS (en '3. Datos procesados'):
    corpus.parquet        corpus normalizado de la etapa 2
    bloques_hcdn.csv      nómina oficial (datos abiertos, desde 10/12/2007)
    nomina_ie103.csv      Informe Especial 103 (1983-2021) -> cubre 2006-2007
                          OJO: tiene que ser un CSV. Si en Drive es una
                          planilla de Google, exportala a CSV con ese nombre.

SALIDAS (en '4. Resultados', que se crea):
    corpus_con_bloque.parquet      un discurso por fila, con diputado y bloque
    metricas_por_intervencion.parquet   + las métricas, sobre el 100% de las filas
    oradores_emparejados.csv       quién es quién, con método y desempate
    oradores_a_revisar.csv         lo que quedó sin resolver, por motivo
    familias_politicas.csv         el mapa bloque nominal -> familia, auditable
    cobertura.csv                  cobertura por período y por año
    resultados_por_bloque.csv      las 4 métricas por bloque nominal
    resultados_por_familia.csv     ídem por familia política
    resultados_por_periodo.csv     ídem por período legislativo
    reconciliacion.txt             de 132.933 discursos a cada subconjunto
    README_resultados.md           qué significa cada columna

DECISIONES QUE ESTÁN IMPLEMENTADAS ACÁ (y por qué):
  * Sin piso de longitud. Toda intervención entra. Una métrica queda en
    NaN sólo cuando su fórmula no está definida (homogeneidad necesita
    10 tokens para 10 segmentos). Nunca se imputa un cero.
  * TTR y homogeneidad dependen fuerte del largo (TTR cae de 0,96 a 0,08
    sobre el MISMO texto leído a 25 y a 4.000 palabras). Se reportan por
    fidelidad al enunciado, pero la comparación entre bloques va por
    MATTR y MTLD, que miden lo mismo sin confundirse con el largo.
  * Coleman-Liau con el signo estándar (-0,296·S). La variante del
    enunciado (+) se guarda aparte para trazabilidad.
  * Se excluye la presidencia: gestiona la sesión, no es oratoria.
  * El bloque se asigna POR FECHA del discurso, no por período.
"""

from __future__ import annotations
import argparse, difflib, glob, json, math, os, re, sys, unicodedata
from collections import Counter
import numpy as np
import pandas as pd

UMBRAL_DIFUSO = 0.86
MIN_TOKENS_HOMOGENEIDAD = 10
VENTANA_MATTR = 25
CORTE_IE = pd.Timestamp('2007-12-09')
BOOTSTRAP = 300

# ---------------------------------------------------------------- texto
PAL = re.compile(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:['´-][0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)*")
ABREV = {"sr","sra","srta","sres","dr","dra","ing","lic","arq","cdor","prof","gral",
         "cnel","tte","cap","art","arts","inc","incs","num","nro","nros","pag","pags",
         "caps","dip","dips","sen","ref","exp","expte","dto","dec","res","cit","etc",
         "ee","uu","aa","vs","pto","ptos","op","min","max","aprox","fs"}

def desacentuar(s): return ''.join(c for c in unicodedata.normalize('NFD', str(s))
                                   if not unicodedata.combining(c))
def norm(s):
    s = desacentuar(s).upper().replace('Ñ', 'N')
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Z ]', ' ', s)).strip()
PARTICULAS = {'DE','DEL','LA','LAS','LOS','Y'}
def nombres_tokens(s): return [p for p in norm(s).split() if p not in PARTICULAS]
def iniciales_de(s): return tuple(p[0] for p in nombres_tokens(s))
def tokenizar(t): return [p.lower() for p in PAL.findall(t)]

def contar_oraciones(texto):
    n = 0
    for m in re.finditer(r'[.!?]+', texto):
        i = m.start()
        if texto[i] == '.':
            der, izq = texto[i+1:i+2], (texto[i-1:i] if i else '')
            if izq.isdigit() and (der.isdigit() or der in '°ºª'):
                continue
            w = re.search(r'([0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)$', texto[:i])
            if w and desacentuar(w.group(1).lower()) in ABREV:
                continue
        n += 1
    return n if n else (1 if PAL.search(texto) else 0)

# ------------------------------------------------------------- métricas
def coleman_liau(letras, pal, ora, signo=-1):
    if pal == 0: return np.nan
    return 0.0588*(100*letras/pal) + signo*0.296*(100*ora/pal) - 15.8

def ttr(t): return len(set(t))/len(t) if t else np.nan

def mattr(t, v=VENTANA_MATTR):
    n = len(t)
    if n < v: return np.nan
    if n == v: return len(set(t))/v
    c = Counter(t[:v]); d = len(c); tot = d
    for i in range(v, n):
        sale, entra = t[i-v], t[i]
        c[sale] -= 1
        if c[sale] == 0: del c[sale]; d -= 1
        if entra not in c: d += 1
        c[entra] += 1
        tot += d
    return tot/((n-v+1)*v)

def _mtld_dir(t, u=0.72):
    f, tipos, n = 0.0, set(), 0
    for w in t:
        tipos.add(w); n += 1
        if len(tipos)/n <= u:
            f += 1; tipos, n = set(), 0
    if n:
        r = len(tipos)/n
        if r < 1.0: f += (1.0-r)/(1.0-u)
    return len(t)/f if f else np.nan

def mtld(t):
    if len(t) < 10: return np.nan
    vals = [v for v in (_mtld_dir(t), _mtld_dir(t[::-1])) if not pd.isna(v)]
    return float(np.mean(vals)) if vals else np.nan

def homogeneidad(t, min_frec=0.001):
    n = len(t)
    if n < MIN_TOKENS_HOMOGENEIDAD: return np.nan
    g = Counter(t)
    vocab = [w for w, c in g.items() if c/n >= min_frec]
    if not vocab: return np.nan
    cortes = [round(i*n/10) for i in range(11)]
    frec = []
    for i in range(10):
        s = t[cortes[i]:cortes[i+1]]; ls = len(s); cs = Counter(s)
        frec.append({w: (cs.get(w, 0)/ls if ls else 0.0) for w in vocab})
    return float(np.mean([max(f[w] for f in frec) - min(f[w] for f in frec)
                          for w in vocab]))

# ------------------------------------------------- familias políticas
# Regla: la familia agrupa denominaciones sucesivas del MISMO espacio.
# No agrupa por coincidencia ideológica ni por interbloque circunstancial.
# Lo que no entra en una regla queda DESCONOCIDA y sale listado para revisión:
# es preferible a forzar un agrupamiento que después no se pueda defender.
REGLAS_FAMILIA = [
    (r'FRENTE PARA LA VICTORIA|UNIDAD CIUDADANA|FRENTE DE TODOS|UNION POR LA PATRIA'
     r'|GRUPO JUSTICIALISTA|KOLINA|NUEVO ENCUENTRO',
     'Peronismo kirchnerista', 'alta'),
    (r'\bPJ\b|JUSTICIALISTA|PERONISTA FEDERAL|PERONISMO FEDERAL|FRENTE PERONISTA'
     r'|FRENTE RENOVADOR|IDENTIDAD PERONISTA|HACEMOS',
     'Peronismo no kirchnerista', 'media'),
    (r'\bUCR\b|UNION CIVICA RADICAL|EVOLUCION RADICAL|DEMOCRACIA PARA SIEMPRE',
     'Radicalismo', 'alta'),
    (r'\bPRO\b|UNION PRO|PROPUESTA REPUBLICANA|CAMBIEMOS|JUNTOS POR EL CAMBIO',
     'PRO / Cambiemos', 'alta'),
    (r'COALICION CIVICA|\bARI\b|CIVICA ARI', 'Coalición Cívica', 'alta'),
    (r'LIBERTAD AVANZA|LIBERTARIO', 'La Libertad Avanza', 'alta'),
    (r'SOCIALISTA|GEN\b|PROGRESISTA|LIBRES DEL SUR|SOLIDARIO',
     'Centroizquierda no peronista', 'media'),
    (r'IZQUIERDA|OBRERO|SOCIALISTA DE LOS TRABAJADORES|PTS|MST',
     'Izquierda', 'alta'),
    (r'MOV POP NEUQUINO|MOVIMIENTO POPULAR NEUQUINO|FRENTE CIVICO POR SANTIAGO'
     r'|PRODUCCION Y TRABAJO|COMPROMISO FEDERAL|SANTA FE FEDERAL|AUTONOMISTA'
     r'|LIBERAL|PROVINCIAL|MISIONES|SALTA|TUCUMAN|RIO NEGRO|SAN LUIS|SAN JUAN',
     'Provinciales', 'media'),
]

def familia_de(nombre):
    n = norm(nombre)
    if not n: return ('SIN BLOQUE', '')
    for patron, fam, conf in REGLAS_FAMILIA:
        if re.search(patron, n):
            return (fam, conf)
    return ('DESCONOCIDA', '')

# ================================================================ 1. nómina
def cargar_nomina(proc):
    bl = pd.read_csv(f'{proc}/bloques_hcdn.csv', dtype=str)
    for c in ('INICIO','FIN','BLOQUE_INICIO','BLOQUE_FIN'):
        bl[c] = pd.to_datetime(bl[c], errors='coerce')     # ISO, sin dayfirst
    bl['FUENTE'] = 'HCDN'
    if 'GENERO' not in bl: bl['GENERO'] = pd.NA
    print(f'nómina HCDN: {len(bl):,} tramos, {bl.ID.nunique():,} IDs')

    cand = [f'{proc}/nomina_ie103.csv', *glob.glob(f'{proc}/**/nomina_ie103*.csv',
                                                   recursive=True)]
    ruta = next((p for p in cand if os.path.isfile(p)), None)
    if ruta is None:
        raise FileNotFoundError(
            f'falta nomina_ie103.csv en {proc}. Si en Drive figura como planilla '
            'de Google, exportala a CSV con ese nombre: una planilla no es un '
            'archivo y la corrida anterior la ignoró en silencio, dejando los '
            'períodos 124-127 (11,8% del corpus) sin bloque.')
    ie = pd.read_csv(ruta, dtype=str)
    for c in ('INICIO','FIN','BLOQUE_INICIO','BLOQUE_FIN'):
        ie[c] = pd.to_datetime(ie[c], errors='coerce')
    assert ie.INICIO.notna().mean() > .95, 'fechas del IE103 mal parseadas'
    # El IE103 no trae GENERO: queda NaN a propósito. filtrar_genero cae a
    # `out or cands`, así que un candidato sin género no se descarta, sólo
    # no desempata. Rellenarlo sería inventar un dato.
    if 'GENERO' not in ie: ie['GENERO'] = pd.NA
    ie['FUENTE'] = 'IE103'
    # El dataset abierto manda DONDE LLEGA, y eso es distinto para cada
    # persona. Un corte fijo en 12/2007 amputaba mandatos reales: Aguad es
    # diputado 2005-2009 (sólo en el Informe) y vuelve 2009-2013 (en el
    # dataset abierto); como figura en las dos fuentes, el corte fijo le
    # borraba 2008 y 2009. Lo mismo con Sarghini, Raimundi, Recalde,
    # Cigogna, Lusquiños y Díaz Roig: ~297.000 palabras, concentradas en
    # los períodos 125-127.
    # Regla correcta: a cada persona presente en las dos fuentes se le
    # recorta el Informe justo donde arranca SU primer mandato en el
    # dataset abierto. Las dos fuentes siguen sin solaparse nunca.
    clave_bl = bl.APELLIDO.map(norm) + '|' + bl.NOMBRE.map(norm)
    inicio_oficial = bl.assign(K=clave_bl).groupby('K').INICIO.min()
    clave_ie = ie.APELLIDO.map(norm) + '|' + ie.NOMBRE.map(norm)
    corte = clave_ie.map(inicio_oficial) - pd.Timedelta(days=1)
    dup = corte.notna()
    solos, repe = ie[~dup], ie[dup].copy()
    c = corte[dup]
    repe = repe[repe.BLOQUE_INICIO <= c]
    c = c.loc[repe.index]
    repe['BLOQUE_FIN'] = repe.BLOQUE_FIN.where(repe.BLOQUE_FIN < c, c)
    repe['FIN'] = repe.FIN.where(repe.FIN < c, c)
    ie = pd.concat([solos, repe], ignore_index=True)
    print(f'nómina IE103: {len(ie):,} tramos usables '
          f'({len(solos):,} exclusivos + {len(repe):,} recortados '
          f'en la fecha propia de cada persona)')
    comunes = [c for c in bl.columns if c in ie.columns]
    assert 'BLOQUE' in comunes, 'el concat perdería BLOQUE'
    return pd.concat([bl[comunes], ie[comunes]], ignore_index=True)

def armar_personas(bl):
    bl = bl.copy()
    bl['AP'] = bl.APELLIDO.map(norm)
    pers = (bl.sort_values('INICIO').groupby('ID')
              .agg(AP=('AP','first'), APELLIDO=('APELLIDO','first'),
                   NOMBRE=('NOMBRE','first'), DISTRITO=('DISTRITO','first'),
                   DESDE=('INICIO','min'), HASTA=('FIN','max')).reset_index())
    alias = {i: i for i in pers.ID}
    def raiz(i):
        while alias[i] != i: i = alias[i]
        return i
    pers['NOM_N'] = pers.NOMBRE.map(norm)
    # misma persona en las dos fuentes
    for _, g in pers.groupby(['AP','NOM_N','DISTRITO']):
        ids = list(g.ID)
        for o in ids[1:]: alias[raiz(o)] = raiz(ids[0])
    # reingresos con ID nuevo: nombre prefijo + mismo distrito + mandatos disjuntos
    for _, g in pers.groupby(['AP','DISTRITO']):
        fs = list(g.itertuples())
        for a, b in ((x, y) for i, x in enumerate(fs) for y in fs[i+1:]):
            ta, tb = nombres_tokens(a.NOMBRE), nombres_tokens(b.NOMBRE)
            corto, largo = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
            if (corto and all(largo[k].startswith(corto[k]) for k in range(len(corto)))
                    and (a.HASTA < b.DESDE or b.HASTA < a.DESDE)):
                alias[raiz(b.ID)] = raiz(a.ID)
    bl['PID'] = bl.ID.map(raiz)
    personas = (bl.sort_values('INICIO').groupby('PID')
                  .agg(AP=('AP','first'), APELLIDO=('APELLIDO','last'),
                       NOMBRE=('NOMBRE','last'), DISTRITO=('DISTRITO','last'),
                       GENERO=('GENERO','last')).reset_index())
    mandatos, tramos = {}, {}
    for pid, g in bl.groupby('PID'):
        mandatos[pid] = sorted(set(zip(g.INICIO, g.FIN)))
        tramos[pid] = sorted(set(zip(g.BLOQUE_INICIO.fillna(g.INICIO),
                                     g.BLOQUE_FIN.fillna(g.FIN), g.BLOQUE)))
    por_ap = {}
    for r in personas.itertuples(index=False):
        por_ap.setdefault(r.AP, []).append(r)
    print(f'  {len(pers):,} IDs -> {len(personas):,} personas')
    return por_ap, sorted(por_ap), mandatos, tramos

# ============================================================ 2. emparejar
def emparejar(corpus, por_ap, APELLIDOS, mandatos, tramos):
    def vigente(pid, f):
        return any(a <= f <= b for a, b in mandatos[pid] if pd.notna(a) and pd.notna(b))
    def bloque_en(pid, f):
        ut = [(a,b,n) for a,b,n in tramos[pid] if pd.notna(a) and pd.notna(b)]
        for a,b,n in ut:
            if a <= f <= b: return n, 'exacto'
        if not ut or not vigente(pid, f): return '', ''
        a,b,n = min(ut, key=lambda t: min(abs((t[0]-f).days), abs((t[1]-f).days)))
        return n, 'tramo más cercano'
    def candidatos(ap):
        if ap in por_ap: return por_ap[ap], 'exacto'
        pre = [a for a in APELLIDOS if a.startswith(ap+' ') or a.endswith(' '+ap)]
        if len(pre) == 1: return por_ap[pre[0]], 'compuesto'
        cor = [a for a in APELLIDOS if ap.startswith(a+' ')]
        if len(cor) == 1: return por_ap[cor[0]], 'compuesto'
        for rep in ('D'+ap, 'DE '+ap):
            if rep in por_ap: return por_ap[rep], 'partícula'
        cerca = difflib.get_close_matches(ap, APELLIDOS, 1, UMBRAL_DIFUSO)
        if cerca and cerca[0][0] == ap[0]: return por_ap[cerca[0]], 'difuso'
        return [], 'sin candidato'
    GEN = {'diputada':'F', 'diputado':'M'}
    def filtrar_genero(c, role):
        g = GEN.get(str(role))
        if not g: return c
        return [x for x in c if str(x.GENERO).upper().startswith(g)] or c
    def filtrar_iniciales(c, paren):
        p = norm(paren)
        if not p: return c
        toks = p.split()
        if all(len(t) == 1 for t in toks):
            ped = tuple(toks); out = []
            for x in c:
                ini = iniciales_de(x.NOMBRE); k = min(len(ped), len(ini))
                if k and ped[:k] == ini[:k]: out.append(x)
            return out or c
        ped = nombres_tokens(p); out = []
        for x in c:
            nom = nombres_tokens(x.NOMBRE); k = min(len(ped), len(nom))
            if k and all(nom[j].startswith(ped[j]) or ped[j].startswith(nom[j])
                         for j in range(k)): out.append(x)
        return out or c
    def canon(ap, paren):
        a, p = norm(ap), norm(paren)
        toks = p.split()
        marca = ''.join(toks) if toks and all(len(t)==1 for t in toks) \
                else ' '.join(nombres_tokens(p))
        return f'{a}|{marca}'

    RUIDO = re.compile(r'PRESI|PROSECRETAR|SECRETARI|LOCUTOR|TAQUIGRAF', re.I)
    dip = corpus[corpus.role.isin(['diputado','diputada'])].copy()
    dip['fecha_dt'] = pd.to_datetime(dip.fecha, errors='coerce')
    dip['canon'] = [canon(a, b) for a, b in zip(dip.speaker_surname.fillna(''),
                                                dip.speaker_paren.fillna(''))]
    ses = (dip.groupby(['canon','role','source_file','periodo','fecha_dt'], dropna=False)
             .agg(speaker_surname=('speaker_surname','first'),
                  speaker_paren=('speaker_paren',
                                 lambda s: max(s.fillna('').astype(str), key=len)),
                  discursos=('canon','size'), palabras=('n_words','sum')).reset_index())
    assert not ses.duplicated(subset=['canon','role','source_file']).any()
    print(f'  {len(ses):,} pares (orador, sesión) a resolver')

    cache, filas, pend = {}, [], []
    for r in ses.itertuples(index=False):
        ap = norm(r.speaker_surname)
        if RUIDO.search(str(r.speaker_surname)) or not ap:
            pend.append((r.canon, r.periodo, 'no es diputado (mesa/locutor)', '',
                         r.discursos, r.palabras)); continue
        if ap not in cache: cache[ap] = candidatos(ap)
        cands, metodo = cache[ap]
        f = r.fecha_dt
        vivos = [c for c in cands if pd.notna(f) and vigente(c.PID, f)]
        if not vivos and (not cands or metodo == 'difuso'):
            pend.append((r.canon, r.periodo, 'sin diputado identificable', '',
                         r.discursos, r.palabras)); continue
        paso = 'fecha'
        if not vivos:
            pend.append((r.canon, r.periodo, 'sin mandato vigente esa fecha',
                         ' | '.join(f'{c.APELLIDO}, {c.NOMBRE}' for c in cands)[:300],
                         r.discursos, r.palabras)); continue
        if len(vivos) > 1:
            vivos = filtrar_iniciales(vivos, r.speaker_paren or ''); paso = 'fecha+iniciales'
        if len(vivos) > 1:
            vivos = filtrar_genero(vivos, r.role); paso += '+género'
        if len(vivos) == 1:
            c = vivos[0]
            filas.append((r.canon, r.role, r.source_file, r.fecha_dt, c.PID,
                          c.APELLIDO, c.NOMBRE, c.DISTRITO, *bloque_en(c.PID, f),
                          metodo, paso, r.discursos, r.palabras))
        else:
            pend.append((r.canon, r.periodo, f'{len(vivos)} homónimos vigentes',
                         ' | '.join(f'{c.APELLIDO}, {c.NOMBRE} [{c.GENERO}]'
                                    for c in vivos)[:300], r.discursos, r.palabras))
    emp = pd.DataFrame(filas, columns=['canon','role','source_file','fecha','id_oficial',
        'apellido','nombre','distrito','bloque','origen_bloque','metodo_apellido',
        'desempate','discursos','palabras'])
    rev = (pd.DataFrame(pend, columns=['canon','periodo','motivo','candidatos',
                                       'discursos','palabras'])
           .groupby(['canon','motivo'], as_index=False)
           .agg(periodos=('periodo', lambda s: ','.join(sorted(set(map(str, s))))),
                candidatos=('candidatos','first'), discursos=('discursos','sum'),
                palabras=('palabras','sum')).sort_values('palabras', ascending=False))

    CLAVE = ['canon','role','source_file']
    tabla = emp.set_index(CLAVE)[['id_oficial','apellido','nombre','distrito','bloque']]
    assert not tabla.index.duplicated().any(), 'clave repetida en el emparejamiento'
    antes = len(dip)
    dip2 = dip.join(tabla, on=CLAVE)
    assert len(dip2) == antes, 'el join duplicó discursos'
    return dip2, emp, rev

# ============================================================ 3. métricas
def calcular_metricas(df):
    col_txt = next((c for c in ('text','texto','speech','discurso','contenido','body')
                    if c in df.columns), None)
    if col_txt is None:
        largos = {c: df[c].astype(str).str.len().mean()
                  for c in df.columns if df[c].dtype == object}
        col_txt = max(largos, key=largos.get)
    print(f'  columna de texto: {col_txt!r}')
    textos = df[col_txt].fillna('').astype(str)
    toks = [tokenizar(t) for t in textos]
    vocab = set().union(*toks) if toks else set()
    print(f'  vocabulario: {len(vocab):,} tipos')

    lemas = None
    try:
        import spacy
        nlp = spacy.load('es_core_news_sm', disable=['parser','ner'])
        v = sorted(vocab)
        lemas = {w: (d[0].lemma_.lower() if len(d) else w)
                 for d, w in zip(nlp.pipe(v, batch_size=2000), v)}
        print('  lemas: spaCy, un pase por tipo')
    except Exception as e:
        print(f'  lemas: no disponibles ({e.__class__.__name__}); sigo sin ellos')
    stems = None
    try:
        import snowballstemmer
        st = snowballstemmer.stemmer('spanish')
        stems = {w: st.stemWord(w) for w in vocab}
    except Exception:
        pass

    out = []
    for t, texto in zip(toks, textos):
        n = len(t)
        letras = sum(len(p) for p in t)
        ora = contar_oraciones(texto)
        tl = [lemas.get(w, w) for w in t] if lemas else t
        ts = [stems.get(w, w) for w in t] if stems else None
        motivos = []
        if n == 0: motivos.append('sin palabras')
        elif n < MIN_TOKENS_HOMOGENEIDAD: motivos.append('homogeneidad indefinida (<10 tokens)')
        if 0 < n < VENTANA_MATTR: motivos.append(f'MATTR indefinido (<{VENTANA_MATTR} tokens)')
        out.append(dict(
            n_palabras=n, n_letras=letras, n_oraciones=ora,
            coleman_liau=coleman_liau(letras, n, ora, -1),
            coleman_liau_variante_enunciado=coleman_liau(letras, n, ora, +1),
            diversidad_cruda=ttr(t),
            diversidad_lema=ttr(tl) if lemas else np.nan,
            diversidad_stem=ttr(ts) if stems else np.nan,
            mattr_lema=mattr(tl), mtld_lema=mtld(tl),
            homogeneidad=homogeneidad(t),
            homogeneidad_lema=homogeneidad(tl) if lemas else np.nan,
            motivo_metrica_nd='; '.join(motivos)))
    met = pd.concat([df.reset_index(drop=True), pd.DataFrame(out)], axis=1)
    assert len(met) == len(df), 'se perdieron filas al medir'
    corto = met[met.n_palabras < 200]
    if len(corto):
        assert corto.coleman_liau.notna().mean() > .99, \
            'hay intervenciones cortas sin Coleman-Liau: quedó un piso escondido'
    return met

# ========================================================== 4. agregación
def ic_bootstrap(valores, grupos, n=BOOTSTRAP, semilla=7):
    """IC 95% de la media remuestreando DIPUTADOS, no intervenciones: las
    intervenciones de un mismo diputado no son independientes."""
    v = pd.Series(valores).reset_index(drop=True)
    g = pd.Series(grupos).reset_index(drop=True)
    ok = v.notna()
    v, g = v[ok], g[ok]
    if v.empty: return (np.nan, np.nan)
    rng = np.random.default_rng(semilla)
    idx = {k: np.array(i) for k, i in v.groupby(g.values).groups.items()}
    claves = np.array(list(idx))
    if len(claves) < 3: return (np.nan, np.nan)
    medias = []
    for _ in range(n):
        sel = rng.choice(claves, len(claves), replace=True)
        medias.append(np.mean(np.concatenate([v.loc[idx[k]].values for k in sel])))
    return (float(np.percentile(medias, 2.5)), float(np.percentile(medias, 97.5)))

METRICAS = ['n_palabras','coleman_liau','mattr_lema','mtld_lema',
            'diversidad_lema','homogeneidad']

def agregar(met, por):
    filas = []
    for clave, g in met.groupby(por, dropna=False):
        f = {por: clave,
             'intervenciones': len(g),
             'diputados': g.id_oficial.nunique(),
             'sesiones': g.source_file.nunique(),
             'palabras': int(g.n_palabras.sum())}
        for m in METRICAS:
            if m not in g: continue
            s = g[m]
            f[f'{m}_n'] = int(s.notna().sum())
            f[f'{m}_media'] = s.mean()
            f[f'{m}_mediana'] = s.median()
            f[f'{m}_desvio'] = s.std()
            f[f'{m}_q1'] = s.quantile(.25)
            f[f'{m}_q3'] = s.quantile(.75)
            if m in ('coleman_liau','mattr_lema'):
                lo, hi = ic_bootstrap(s, g.id_oficial)
                f[f'{m}_ic95_bajo'], f[f'{m}_ic95_alto'] = lo, hi
                w = g.loc[s.notna(), 'n_palabras']
                f[f'{m}_media_ponderada_palabras'] = (
                    np.average(s.dropna(), weights=w) if w.sum() else np.nan)
        filas.append(f)
    out = pd.DataFrame(filas).sort_values('palabras', ascending=False)
    # no publicar estadísticos sin base: se marcan, no se borran
    out['base_suficiente'] = (out.intervenciones >= 30) & (out.diputados >= 3)
    return out

# ================================================================= main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True)
    a = ap.parse_args()
    base = a.base.rstrip('/')
    proc = next((p for p in (f'{base}/3. Datos procesados', f'{base}/Stage2_salida')
                 if os.path.isdir(p)), None)
    assert proc, f'no encuentro la carpeta de datos procesados en {base}'
    res = f'{base}/4. Resultados'
    os.makedirs(res, exist_ok=True)

    print('\n=== 1. nómina ===')
    bl = cargar_nomina(proc)
    por_ap, APELLIDOS, mandatos, tramos = armar_personas(bl)

    print('\n=== 2. emparejamiento ===')
    corpus = pd.read_parquet(f'{proc}/corpus.parquet')
    n_corpus, pal_corpus = len(corpus), int(corpus.n_words.sum())
    print(f'corpus: {n_corpus:,} discursos, {pal_corpus:,} palabras, '
          f'{corpus.speaker_key.nunique():,} oradores')
    if corpus.speaker_key.nunique() > 1500:
        print('  *** más de 1.500 oradores: parece el corpus PREVIO a la '
              'corrección de diario_2009091610. Verificalo antes de publicar. ***')
    dip2, emp, rev = emparejar(corpus, por_ap, APELLIDOS, mandatos, tramos)
    dip2.to_parquet(f'{res}/corpus_con_bloque.parquet', index=False)
    emp.to_csv(f'{res}/oradores_emparejados.csv', index=False, encoding='utf-8')
    rev.to_csv(f'{res}/oradores_a_revisar.csv', index=False, encoding='utf-8')

    pal_dip = int(dip2.n_words.sum())
    con_dip = int(dip2.loc[dip2.id_oficial.notna(), 'n_words'].sum())
    con_blo = int(dip2.loc[dip2.bloque.notna() & (dip2.bloque.astype(str) != ''),
                           'n_words'].sum())
    assert con_blo <= con_dip, 'más palabras con bloque que con diputado'
    print(f'  diputado identificado: {100*con_dip/pal_dip:.1f}% de las palabras')
    print(f'  bloque asignado      : {100*con_blo/pal_dip:.1f}%')
    f_dip = pd.to_datetime(dip2.fecha, errors='coerce')
    tiene = dip2.bloque.notna() & (dip2.bloque.astype(str) != '')
    for etiqueta, mask in [('2006-2007', f_dip < '2007-12-10'),
                           ('2008-2009', (f_dip >= '2007-12-10') & (f_dip < '2010-01-01'))]:
        if mask.any():
            print(f'  cobertura {etiqueta}  : {100*tiene[mask].mean():.1f}% '
                  f'de las intervenciones')
    cob_pre = tiene[f_dip < '2007-12-10'].mean() if (f_dip < '2007-12-10').any() else 1
    if cob_pre < .5:
        print('  *** el tramo 2006-2007 quedó sin bloque: revisá que '
              'nomina_ie103.csv sea el archivo correcto ***')

    print('\n=== 3. métricas (sin piso de longitud) ===')
    met = calcular_metricas(dip2)
    fam = met.bloque.fillna('').map(familia_de)
    met['familia'] = [f for f, _ in fam]
    met['familia_confianza'] = [c for _, c in fam]
    met.to_parquet(f'{res}/metricas_por_intervencion.parquet', index=False)

    mapa = (met.groupby('bloque', dropna=False)
              .agg(intervenciones=('n_palabras','size'), palabras=('n_palabras','sum'),
                   desde=('fecha','min'), hasta=('fecha','max')).reset_index())
    mapa[['familia','confianza']] = [list(familia_de(b)) for b in mapa.bloque.fillna('')]
    mapa.sort_values('palabras', ascending=False).to_csv(
        f'{res}/familias_politicas.csv', index=False, encoding='utf-8')
    desc = mapa[mapa.familia == 'DESCONOCIDA']
    print(f'  bloques nominales: {len(mapa)} | sin familia asignada: {len(desc)} '
          f'({100*desc.palabras.sum()/max(mapa.palabras.sum(),1):.1f}% de las palabras)')

    print('\n=== 4. agregación ===')
    agregar(met, 'bloque').to_csv(f'{res}/resultados_por_bloque.csv', index=False)
    agregar(met, 'familia').to_csv(f'{res}/resultados_por_familia.csv', index=False)
    agregar(met, 'periodo').to_csv(f'{res}/resultados_por_periodo.csv', index=False)

    met['anio'] = pd.to_datetime(met.fecha, errors='coerce').dt.year
    tmp = met.copy()
    tmp['con_bloque'] = tmp.bloque.notna() & (tmp.bloque.astype(str) != '')
    tmp['pal_cb'] = tmp.n_palabras.where(tmp.con_bloque, 0)
    cob = (tmp.groupby(['periodo','anio'])
              .agg(intervenciones=('n_palabras','size'), palabras=('n_palabras','sum'),
                   palabras_con_bloque=('pal_cb','sum')).reset_index())
    cob['cobertura_pct'] = 100*cob.palabras_con_bloque/cob.palabras
    cob.to_csv(f'{res}/cobertura.csv', index=False)

    with open(f'{res}/reconciliacion.txt', 'w', encoding='utf-8') as f:
        for linea in [
            f'corpus.parquet                     {n_corpus:>9,} discursos  {pal_corpus:>12,} palabras',
            f'- presidencia y mesa (role)        {n_corpus-len(dip2):>9,}',
            f'= intervenciones de diputados      {len(dip2):>9,}            {pal_dip:>12,}',
            f'  con diputado identificado        {int(dip2.id_oficial.notna().sum()):>9,}            {con_dip:>12,}',
            f'  con bloque asignado              {int((dip2.bloque.notna() & (dip2.bloque.astype(str)!="")).sum()):>9,}            {con_blo:>12,}',
            f'  con homogeneidad calculable      {int(met.homogeneidad.notna().sum()):>9,}',
            f'  con MATTR calculable             {int(met.mattr_lema.notna().sum()):>9,}',
            'Coleman-Liau y total de palabras se calculan sobre el 100% de las filas.',
            'Ninguna intervención se excluye por ser corta.']:
            print(linea); f.write(linea + '\n')

    with open(f'{res}/README_resultados.md', 'w', encoding='utf-8') as f:
        f.write(
"""# Resultados

Una fila por bloque / familia / período. Para cada métrica: `_n` (intervenciones
donde está definida), media, mediana, desvío, cuartiles. Para Coleman-Liau y
MATTR, además IC 95% por bootstrap remuestreando **diputados** (las
intervenciones de un mismo diputado no son independientes) y la media ponderada
por palabras como control.

**Qué comparar entre bloques:** `mattr_lema` y `mtld_lema` para diversidad,
`coleman_liau` para legibilidad, `n_palabras` para longitud.

**Qué NO comparar crudo:** `diversidad_lema` (TTR) y `homogeneidad`. Son las
definiciones del enunciado y se reportan por fidelidad, pero dependen del largo
del texto: sobre un mismo texto leído a 25 y a 4.000 palabras, el TTR cae de
0,96 a 0,08 y la homogeneidad de 0,40 a 0,008. Promediadas por bloque miden
cuán largo habla cada bloque. MATTR sobre ese mismo texto se mueve menos del 2%.

`homogeneidad` queda vacía cuando la intervención tiene menos de 10 palabras
(la fórmula parte el texto en 10 segmentos). `mattr_lema`, cuando tiene menos de
25. El motivo está en `motivo_metrica_nd`. No hay piso de inclusión: toda
intervención entra en el archivo y en el conteo de palabras y legibilidad.

`base_suficiente` marca las filas con al menos 30 intervenciones y 3 diputados.
Las demás se publican igual, pero sus medias son imprecisas.

`familias_politicas.csv` es el mapa auditable de bloque nominal -> familia, con
nivel de confianza. Lo que quedó en DESCONOCIDA hay que resolverlo a mano antes
de usar `resultados_por_familia.csv`.
""")
    print(f'\nListo. Todo en: {res}')

if __name__ == '__main__':
    main()
