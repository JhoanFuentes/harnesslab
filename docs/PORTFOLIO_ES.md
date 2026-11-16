# Presentar HarnessLab como trabajo de ingeniería

El valor del portafolio está en demostrar criterio: formular un problema, construir una solución verificable, detectar límites y explicar decisiones. Un repositorio por sí solo no permite estimar cuánto cambiarán tus probabilidades de contratación. La meta práctica es dar evidencia útil a un entrevistador de Senior AI Engineer / Applied AI.

## Historia del proyecto

**Problema:** un agente puede producir una conclusión convincente, perder progreso tras una interrupción o proponer una acción sin haber satisfecho los controles del sistema.

**Solución:** HarnessLab separa las decisiones del modelo del runtime que valida acciones, conserva estado, administra límites y registra qué ocurrió. El caso aplicado es el diagnóstico de incidentes sintéticos; las acciones con efecto son simulaciones locales.

**Evidencia:** evaluaciones deterministas de contratos, pruebas de recuperación y comparaciones entre perfiles. El adaptador opcional de OpenAI Responses permite investigar calidad con un modelo real, pero esa calidad solo debe presentarse como medida después de ejecutar y documentar el experimento.

Pitch de 30 segundos, adaptable a lo que realmente hayas trabajado:

> HarnessLab es un laboratorio de confiabilidad para agentes que diagnostican incidentes. El modelo propone acciones; un runtime en Python valida contratos, conserva sesiones en SQLite y controla evidencia, reintentos y aprobaciones locales. Separé las pruebas deterministas del harness de las evaluaciones con modelos reales, para poder distinguir una falla de ingeniería de una decisión incorrecta del modelo. Mi contribución específica fue [cambio propio], y la validé con [experimento verificable].

No rellenes los corchetes con experiencia inventada. Si aún no agregaste una contribución propia, explica que estás estudiando y extendiendo una base construida con asistencia de IA.

## Demo de cinco minutos

Prepara los comandos vigentes del [README](../README.md) en una terminal limpia. Ejecuta antes la secuencia completa y confirma qué archivos o sesiones necesita. La demo offline evita depender de conectividad, saldo o disponibilidad de un proveedor.

| Tiempo | Qué mostrar | Qué debe quedar claro |
| --- | --- | --- |
| 0:00–0:35 | Problema y esquema de componentes. | El harness es software con responsabilidades explícitas alrededor de un modelo reemplazable. |
| 0:35–1:20 | Un caso normal y su resultado. | La conclusión se puede relacionar con observaciones registradas. |
| 1:20–2:10 | Un caso que activa el evidence gate o la aprobación. | El control cambia el comportamiento observable; una cita válida sigue sin probar verdad semántica. |
| 2:10–3:00 | Una falla transitoria o una sesión interrumpida y reanudada. | La persistencia y los límites tienen efectos demostrables. |
| 3:00–4:05 | Reporte por perfil y una trayectoria. | El score agregado tiene explicación; los resultados scripted son pruebas del runtime. |
| 4:05–5:00 | Tu extensión, una decisión y una limitación. | Puedes defender lo que construiste y reconocer lo que falta para producción. |

Si un escenario no está disponible en la versión que publicas, sustituye ese segmento por uno comprobado. No simules una ejecución live con una salida scripted ni presentes un video editado como una corrida continua.

## README que un reviewer pueda evaluar rápido

La primera pantalla debería responder: qué problema resuelve, qué evidencia incluye y cómo ejecutarlo. Después, presenta la arquitectura y las limitaciones. Una secuencia útil:

1. Una frase de propósito y una imagen real del reporte o una demo breve.
2. Quickstart offline probado desde un entorno limpio.
3. Qué garantiza el runtime y qué queda a cargo del modelo.
4. Resultados observados, enlazados a un artefacto reproducible, con fecha y configuración.
5. Arquitectura, threat model y metodología de evaluación.
6. Contribuciones y siguientes experimentos concretos.

Evita frases como “production-ready”, “elimina alucinaciones”, “SOTA”, “enterprise security” o “100 % confiable” si la evidencia no las sostiene. La aprobación por CLI no implementa identidad empresarial. Las herramientas locales simuladas no prueban una integración de producción.

## Publicación en GitHub

1. Revisa licencia, instrucciones y archivos que vas a subir. Excluye credenciales, entornos virtuales, bases de sesiones privadas y logs con información sensible. Los fixtures sintéticos y reportes seleccionados deben estar identificados como tales.
2. Crea el repositorio con un nombre claro, por ejemplo `harnesslab`, y una descripción breve: “Durable agent runtime and behavioral evaluation lab for incident diagnosis”. Esta sugerencia no asegura disponibilidad del nombre.
3. Sube un estado inicial que pase las comprobaciones documentadas. No fabriques meses de commits ni atribuyas retrospectivamente trabajo no realizado.
4. Comprueba el quickstart tras clonar en un directorio nuevo. Revisa el estado real de CI antes de publicar una insignia que afirme que pasa.
5. Adjunta una release con la versión que demostraste y sus resultados. Explica si el reporte es offline/scripted o live y conserva la configuración correspondiente.
6. Abre issues pequeños para trabajo real pendiente. Convierte tu extensión de la semana cuatro en un PR con revisión y resultados.
7. Fija el repositorio en tu perfil y enlaza una demo corta. Una imagen del reporte real comunica más que un diagrama que anuncie componentes inexistentes.

No necesitas publicar acceso a un proveedor ni un servicio con escritura real para hacer el proyecto evaluable. Si más adelante añades un despliegue, describe su alcance y costos por separado.

## CV y LinkedIn: plantillas honestas

Usa estas frases solo después de verificar cada afirmación. Sustituye los campos con valores medidos o elimina la parte cuantitativa.

- “Desarrollé y extendí un runtime de agentes en Python con persistencia SQLite, validación de acciones y recuperación de sesiones; comprobé sus contratos mediante [número real] pruebas y escenarios reproducibles.”
- “Diseñé una evaluación de incidentes sintéticos que separa calidad del diagnóstico, evidencia, comportamiento de política y fallas de herramientas; publiqué resultados por caso y configuración.”
- “Comparé [configuraciones reales] sobre [casos/trials reales] y documenté [hallazgo observado], junto con costos, latencia y límites de generalización.”
- “Implementé [contribución propia] y añadí una prueba de regresión que reproduce [falla concreta].”

La primera frase requiere haber desarrollado o extendido y comprendido realmente esa parte. Si tu trabajo principal fue análisis y validación, escribe “Evalué y extendí…” o “Reproduje y analicé…”. No transformes un proyecto personal en experiencia laboral o impacto en clientes.

Para una publicación técnica, cuenta un solo hallazgo con una trayectoria y un enlace al experimento. Por ejemplo: “Un gate de evidencia rechazó referencias inexistentes, pero aceptó una conclusión equivocada con IDs válidos. Este caso muestra la diferencia entre procedencia y soporte semántico”. Publica ese hallazgo únicamente cuando hayas ejecutado el caso correspondiente.

## Preguntas que conviene poder responder

| Pregunta | Evidencia que puedes mostrar |
| --- | --- |
| ¿Por qué no bastó con mejorar el prompt? | Un contrato que debe cumplirse incluso cuando el proveedor propone una acción incorrecta. |
| ¿Qué pasa después de un crash? | Estado persistido, eventos y un escenario de recuperación reproducible. |
| ¿Cómo sabes que la evaluación no se aprueba bloqueando todo? | Controles positivos y negativos, más inspección del resultado real. |
| ¿Qué aporta cada mecanismo? | Una ablation de una sola variable y su trayectoria. |
| ¿Qué parte es IA aplicada y cuál es ingeniería de sistemas? | Adaptador y evaluación live por un lado; persistencia, validación y ejecución por otro. |
| ¿Qué harías antes de un piloto con clientes? | Un plan priorizado que contemple datos, autorizaciones, integraciones, observabilidad y criterios de salida. |
| ¿Qué aprendiste personalmente? | Un issue, un PR y un experimento cuyo resultado cambió una decisión tuya. |

El resultado más convincente es una contribución pequeña que puedas explicar a fondo, acompañada de evidencia que otra persona pueda reproducir.
