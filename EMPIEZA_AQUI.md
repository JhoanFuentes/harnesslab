# HarnessLab: guía de lectura y demostración

**HarnessLab** es un laboratorio ejecutable de confiabilidad de agentes. Su caso
aplicado es diagnosticar incidentes consultando evidencia. La parte senior está
en diseñar y demostrar contratos: qué puede hacer el agente, qué tiene que
verificar, qué ocurre si falla y cómo sabes si mejoró.

La validación local registrada incluye **69 pruebas aprobadas** y **48 ejecuciones
scripted**: 12 escenarios sobre cuatro configuraciones. El adaptador para un
modelo real está implementado y probado con respuestas HTTP simuladas; la calidad
de diagnóstico de un LLM real aún debe medirse.

Esa cifra corresponde a la versión publicada original. La nueva extensión agrega
planes congelados, comparación de modelos, repeticiones por caso y costo por tarea
exitosa incluyendo los intentos fallidos. Está preparada **sin llamadas pagadas**.
Consulta [el nuevo protocolo](docs/LIVE_STUDY.md) y el registro de validación para
las comprobaciones de esta ampliación.

Puedes generar su vista previa sin clave ni red:

```sh
python -m harnesslab study-plan --config studies/live.example.json --output runs/study-plan.json
```

Abre `runs/study-plan.html`. Describe 240 intentos previstos y deja pendientes los
modelos y tarifas. No es un reporte de precisión o costos medidos de un LLM.

## Cómo presentarlo en 30 segundos

> HarnessLab estudia la confiabilidad de un agente que diagnostica incidentes.
> Separa las decisiones del modelo de la persistencia, las aprobaciones y la
> validación de evidencia. Sus evaluaciones permiten observar qué cambia al
> retirar un control y seguir cada resultado hasta su trayectoria. La demo
> reproducible mide contratos del runtime; una evaluación live separada medirá
> la calidad del modelo.

Después del resumen, muestra un fallo concreto, el control que lo contiene y su
prueba de regresión. Añade tu contribución personal únicamente cuando la hayas
implementado y puedas explicar sus resultados.

## Primero, míralo funcionar

1. Abre [el reporte público](https://jhoanfuentes.github.io/harnesslab/) en tu navegador.
   También tienes [la copia local](examples/demo/report.html).
2. Desde esta carpeta, con Python 3.11 o superior, ejecuta:

```sh
python -m harnesslab demo --output runs/demo
python -m unittest discover -s tests -v
```

No necesitas claves, paquetes de terceros ni una cuenta de pago para la demo.
Si Windows no encuentra Python, instala Python 3.11+ o usa la ruta de un
intérprete existente. El proyecto se verificó aquí con Python 3.12.14 incluido
en el entorno de Codex; no depende de esa distribución.

Para orientarte en el código: empieza por [el loop](harnesslab/engine.py), continúa
con [las transacciones y recibos](harnesslab/store.py) y termina en
[los graders](harnesslab/evaluation.py). Las
[decisiones de arquitectura](docs/ARCHITECTURE.md) explican los límites de cada
componente; el [registro de validación](docs/VALIDATION.md) distingue lo ejecutado
localmente de lo pendiente.

## Qué demostrar en una entrevista

- Una respuesta convincente puede citar evidencia inexistente. Muestra cómo el
  gate la rechaza y cómo un grader independiente detecta el problema sin el gate.
- Un proceso puede morir después de una acción. Muestra el recibo persistido y
  la recuperación sin duplicar el efecto local.
- Una llamada al modelo puede consumir dinero aunque se pierda su respuesta.
  Explica por qué el sistema conserva la incertidumbre y no reintenta a ciegas.
- Las instrucciones de un documento no equivalen a autorización. Muestra la
  aprobación ligada a una acción, sus argumentos y la sesión.
- Un 12/12 de pruebas scripted significa que esos contratos pasaron. No significa
  que el modelo tenga 100 % de precisión sobre incidentes reales.

## Tu siguiente contribución

Completa la [ruta práctica de cuatro semanas](docs/LEARNING_PATH_ES.md). La
extensión con más relación con Applied AI es un conjunto de incidentes held-out,
sin pistas explícitas de respuesta, con etiquetas revisadas y evaluación live.
Publica también errores, costo, latencia y decisiones cambiadas por los resultados.

El [README técnico](README.md) está en inglés para facilitar su revisión por
equipos internacionales. Los [guiones de demo y CV](docs/PORTFOLIO_ES.md) están
en español. Usa [la guía de publicación](docs/PUBLISH_ES.md) para subirlo cuando
hayas revisado el contenido y confirmar el estado de CI en tu repositorio.

La base se construyó con asistencia de IA. Comprenderla, revisarla y hacer una
contribución propia medible es lo que te permitirá defenderla con credibilidad.
No hace falta inventar experiencia de producción ni métricas de contratación.
