Metodología
Midiendo sus palabras: evolución del lenguaje en el Congreso argentino
Este documento registra cada decisión que se apartó del diseño del estudio fundacional o
que requirió elegir entre alternativas, con su justificación y, cuando la hubo, la
evidencia que la respaldó.
---
1. Obtención
Un programa en Python recorrió el índice oficial de Diarios de Sesiones de la HCDN
(`https://www.hcdn.gob.ar/secparl/dtaqui/diario-de-sesiones.html`) período por período,
detectó las reuniones de cada uno y descargó los PDF con el patrón
`https://www4.hcdn.gob.ar/dependencias/dtaquigrafos/diarios/periodo-NNN/diario_AAAAMMDDR.pdf`,
con hasta tres intentos por variante de dirección. Cada intento quedó en
`log_descarga.txt` y cada archivo en `manifest.csv`. La verificación del certificado SSL
del sitio estaba desactivada. [COMPLETAR: motivo.]
Resultado: 459 PDF de 462 reuniones existentes. La completitud se verificó por dos vías
independientes (ver 5.1).
---
2. Normalización
2.1 Separación de los documentos insertos por tipografía
El Diario de Sesiones intercala en el texto de la sesión documentos que no son discurso:
proyectos de ley, resoluciones, inserciones solicitadas por los diputados. Separarlos por
palabras clave resultó frágil. En cambio, el registro compone la oratoria en un tamaño de
letra y los documentos en uno menor: 11 y 9,5 puntos en todas las épocas muestreadas.
El programa no fija esos valores: aprende el tamaño de la oratoria en cada documento
a partir de los párrafos que empiezan con un encabezado de orador. La decisión se
justificó en los datos: tres sesiones de 2025 compusieron la oratoria en 12 puntos y una
de 2022 en 11,5. Un valor fijo las habría perdido sin aviso.
Cuando dos tamaños compiten, se desempata hacia el mayor, porque el registro nunca
compone la oratoria más chica que los documentos que interpola. Esta regla corrigió la
única sesión mal procesada del corpus (16/09/2009), cuyo apéndice reproducía oratoria
citada en letra chica y había invertido la calibración. La corrección eliminó además 181
nombres de orador que sólo existían en ese apéndice.
2.2 Corte en intervenciones
La unidad es la intervención ininterrumpida: desde un encabezado de orador hasta el
siguiente. Un encabezado abre siempre un párrafo nuevo, y esa regla se aplica con
independencia de la geometría de la página, que varía a lo largo de veinte años de
diarios. Es lo que garantiza que ninguna intervención quede absorbida por la anterior.
Las acotaciones de la sala (`(Aplausos.)`) se eliminan por no ser discurso. Los
paréntesis del propio orador se conservan.
2.3 Dos defectos de los PDF que corrompían el texto sin aviso
Rotación de página. La función de PyMuPDF que endereza páginas rotadas recorta el
texto contra el borde visible y elimina sílabas en mitad de palabra. Se reemplazó por
una transformación de coordenadas que no modifica la página.
Ligaduras partidas. Grupos como fi llegaban separados (`identifi cación`): más de
1.200 palabras en una sola sesión de 2016. Inflaban el conteo de palabras y llenaban el
vocabulario de fragmentos, lo que habría distorsionado a la vez legibilidad, diversidad
y homogeneidad. Se reparan antes de cualquier cálculo.
2.4 Control de calidad
Cada encabezado de orador ocupa una línea completa, así que contar las líneas que empiezan
con uno da una cifra de intervenciones independiente del procesamiento, tomada antes
de reconstruir columnas o párrafos. Se compara con las intervenciones emitidas en cada
sesión. Este control detectó todos los errores encontrados durante el desarrollo.
Sobre las 459 sesiones, las diferencias se repartieron así: en 42 sesiones el programa
encontró 296 intervenciones más que el control, porque captura encabezados partidos por un
corte de línea que el conteo línea por línea no ve; en 17 sesiones encontró 20 menos. La
pérdida real es de 20 intervenciones sobre 132.933 (0,015 %).
---
3. Identificación de oradores y bloques
3.1 Bloque asignado por fecha, no por período
Los diputados argentinos cambian de bloque con frecuencia durante un mismo mandato. Por
eso cada intervención recibe el bloque cuyo intervalo de pertenencia contiene la fecha
de la sesión, y no un bloque único por período.
3.2 Dos nóminas oficiales
El dataset abierto de legisladores de la HCDN registra mandatos sólo desde el 10/12/2007;
un diputado con mandato 2005–2009 no figura aunque haya hablado en 2008. Los años
anteriores se cubrieron con el Informe Especial N.º 103 de la Dirección General de
Información Parlamentaria, que lista la composición de la Cámara por bienio con bloque y
fechas. Donde ambas fuentes se superponen, manda el dataset abierto.
3.3 Cómo se resolvieron los homónimos y las variantes
El Diario identifica a los oradores por apellido, a veces con iniciales. El emparejamiento
se hace por orador y por sesión, y usa en orden:
La fecha. De los 84 apellidos repetidos en la nómina, 43 nunca tuvieron dos
diputados en ejercicio a la vez.
Las iniciales, cuando el taquígrafo las consigna.
El género. El taquígrafo escribe Sr. o Sra., y la nómina registra el género.
Esto resolvió casos de mucho peso, como Cecilia y Leopoldo Moreau.
Apellidos compuestos abreviados (`Donda` por DONDA PÉREZ).
Erratas, con una comparación tolerante condicionada a dos requisitos: que el
candidato tuviera mandato vigente ese día y que coincida la primera letra del
apellido. Sin el segundo, `Ferro` se habría emparejado con ERRO —dos personas
distintas— y le habría atribuido 18.147 palabras a quien no las dijo.
Antes de emparejar se unificaron las variantes de escritura del propio corpus (`Godoy (J. C. L.)`, `Godoy (J.C.L.)`, `Godoy (J.C.L)`), y en la nómina se unificaron los
diputados a los que la HCDN asignó un identificador nuevo al volver tras un intervalo,
exigiendo coincidencia de distrito.
El programa se detiene si la unión con las nóminas duplica discursos o si las palabras
con bloque superan a las palabras con diputado identificado, dos síntomas de un error de
unión que se detectó y corrigió durante el desarrollo.
3.4 Familias políticas
Los bloques nominales cambian de nombre con frecuencia. Para la comparación de largo plazo
se agruparon en familias políticas (por ejemplo, peronismo kirchnerista, radicalismo,
PRO/Cambiemos). [COMPLETAR: criterio de agrupamiento y fuente o justificación de cada
asignación.]
---
4. Métricas
4.1 Qué se excluye
Se excluyen las intervenciones de la presidencia y la mesa: son 74.174 de 132.933 filas
y su contenido es gestión de la sesión («tiene la palabra el señor diputado»), no
oratoria. No se excluye ninguna intervención de diputados por ser corta. Cuando una
fórmula no está matemáticamente definida para un texto (por ejemplo, la homogeneidad con
menos de diez palabras), el valor queda vacío y se registra el motivo; nunca se imputa.
La unidad de análisis es la intervención, sin unir las intervenciones de un mismo orador
en una sesión, por fidelidad al registro taquigráfico.
4.2 Coleman–Liau: el signo de la fórmula
El enunciado del proyecto escribe el término de oraciones con signo positivo. La fórmula
original de Coleman y Liau (1975) lo resta:
```
CLI = 0,0588 · L − 0,296 · S − 15,8
L = letras cada 100 palabras      S = oraciones cada 100 palabras
```
Con el signo positivo el índice se invierte: las oraciones cortas puntúan como más
difíciles, lo contrario de lo que mide. Sobre el corpus, la versión estándar da una media
de 11,73 (desvío 1,39) y la del enunciado 14,08 (desvío 1,24), con menor capacidad de
distinguir textos. Se usa la fórmula original y se conserva la variante del enunciado en
una columna aparte para que la decisión sea verificable.
El índice está calibrado sobre textos en inglés. Se reporta como índice comparable entre
bloques y a lo largo del tiempo, nunca como grado de escolaridad.
Como el número de oraciones es el denominador del índice, el corte de oraciones protege
dos casos: las abreviaturas habituales del Diario (Sr., Dra., art., inc., unas
cincuenta en total) y el punto como separador de miles (`1.500 millones`). Control: la
mediana resultante es de 25,9 palabras por oración, dentro de lo esperable para prosa
parlamentaria en castellano.
4.3 Lematización en lugar del stemmer de CoreNLP
El estudio fundacional reduce las palabras a su raíz con CoreNLP, que no tiene herramienta
para castellano. Se calcularon dos alternativas: lemas de spaCy (`es_core_news_sm`) y
raíces del algoritmo Snowball para español. Correlacionan 0,986; se reporta la versión con
lemas y la otra queda como control. Por eficiencia, cada palabra distinta del corpus se
lematiza una vez y el resultado se aplica a todas sus apariciones.
4.4 Diversidad y homogeneidad dependen del largo del texto
Este es el hallazgo metodológico central de la investigación.
Se tomaron 300 discursos de más de 2.000 palabras y se midieron leyendo sólo sus primeras
25, 50, 100… palabras. Mismo texto, mismo orador; sólo cambia cuánto se lee. La
homogeneidad se reduce a la mitad cada vez que el largo se duplica (de 0,41 con 25
palabras a 0,01 con 2.000), y la diversidad tal como la define el estudio fundacional
(palabras distintas sobre palabras totales) cae de forma sostenida.
La causa es aritmética. En la homogeneidad, una palabra que aparece una vez en un texto de
25 palabras representa el 40 % de su segmento; en uno de 2.000, el 0,5 %. En la diversidad,
las palabras más comunes se agotan rápido y el cociente baja con el largo.
Como la extensión de las intervenciones varía con el lugar en el debate, la condición de
oficialismo u oposición y el tiempo concedido, comparar estas métricas sin corregir habría
medido cuánto habla cada bloque, no cómo habla, y el resultado habría pasado por un
hallazgo sobre el lenguaje.
Corrección adoptada para la diversidad: se usan dos medidas diseñadas para no depender
del largo, MATTR (el mismo cociente, calculado sobre una ventana móvil de 25 palabras) y
MTLD (McCarthy y Jarvis, 2010, con el umbral estándar de 0,72). La definición original se
conserva en los datos por fidelidad al enunciado, pero no se usa para comparar.
Homogeneidad: [VERIFICAR contra `pipeline_final.py`: cómo se controla el largo en la
comparación, por ejemplo con ventanas de igual tamaño o reportándola por tramos de
extensión.]
4.5 Topic words
El enunciado original del proyecto incluía el análisis de topic words. Quedó fuera del
análisis por decisión del autor. [COMPLETAR: motivo.]
---
5. Limitaciones
Todas las limitaciones de esta sección son externas al procesamiento o están cuantificadas,
y para la más relevante se verificó empíricamente que no sesga los resultados.
---
5.1 Cobertura documental
Tres sesiones no publicadas por la HCDN
Período	Reunión	Fecha
125	15.ª	11/07/2007
130	14.ª	08/08/2012
132	7.ª	21/05/2014 (segunda reunión de ese día)
Las tres devuelven error 404 en el sitio y su página de sesión no ofrece versión
taquigráfica. Representan el 0,65 % de las 462 sesiones del período. Pueden solicitarse a
la Dirección de Taquígrafos.
Que no falte ninguna otra se verificó de dos formas. La HCDN numera las reuniones por
sesión celebrada —incluso las que fracasan por falta de quórum reciben número— y la
numeración del corpus es contigua en todos los períodos salvo esos tres huecos. Además, el
número máximo de reunión coincide con el listado oficial `hcdn.gob.ar/sesiones` en los 19
períodos que ese listado cubre (125 a 143). El período 124 no figura allí, pero su
numeración es contigua del 1 al 58 y la última sesión es del 27/02/2007, un día antes del
cierre del período.
Cinco documentos escaneados del período 124
Cinco diarios de marzo a mayo de 2006 son imágenes de página sin capa de texto (unos
1,45 GB). Se decidió no aplicar reconocimiento óptico de caracteres: sus errores
contaminarían precisamente la legibilidad y la diversidad léxica, dos de las métricas
del estudio.
Una sesión sin discursos
El diario del 10/03/2010 registra una sesión especial que fracasó por ausencia de los
diputados. Cero intervenciones es el resultado correcto.
En total se procesaron 453 sesiones de las 462 existentes.
---
5.2 Identificación de oradores
Alrededor del 1 % de las palabras sin diputado identificado
Son unas 940 intervenciones y 176.000 palabras, por dos razones: apellidos repetidos con
varios diputados en ejercicio a la vez donde ni la fecha ni el género alcanzaron para
desempatar (López, Martínez, Gutiérrez, entre otros), y una veintena de oradores que no
figuran en ninguna de las dos nóminas. Están dispersos a lo largo de los veinte años y de
todos los bloques.
Verificación de que no sesga. Al incorporar el Informe Especial 103 se recuperaron
282.611 palabras de 2006–2009 que antes quedaban sin bloque. Se repartieron entre varios
espacios políticos y ninguna media por bloque se movió más allá del tercer decimal (por
ejemplo, la legibilidad del peronismo kirchnerista pasó de 11,0838 a 11,0967). Un
faltante de esa magnitud no altera las conclusiones.
3,2 % de las palabras en bloques sin familia política asignada
Son monobloques y partidos de distrito pequeños. No afectan los resultados por bloque
nominal ni por período; en la tabla por familia política figuran como categoría residual
declarada.
---
5.3 Medición
Las decisiones de medición que pueden discutirse —el signo de Coleman–Liau, su calibración
en inglés, la lematización en lugar del stemmer de CoreNLP, la dependencia del largo de
la diversidad y la homogeneidad, y la exclusión de los topic words— están desarrolladas
en la sección 4. No se listan aquí como limitaciones porque cada una tiene una
resolución documentada.
