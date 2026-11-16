# Ruta práctica: Senior AI Engineer / Applied AI

Objetivo: poder modificar HarnessLab, explicar sus decisiones y defender sus límites sin depender de un guion. El repositorio es un punto de partida; el aprendizaje demostrable aparece cuando agregas casos, encuentras fallas y publicas experimentos propios.

Plan sugerido: cuatro semanas, entre seis y diez horas por semana. Ajusta el ritmo a tu experiencia. Usa los comandos del [README](../README.md), que es la fuente de verdad de la interfaz, y conserva resultados con fecha y revisión del código.

## Semana 1: entender el contrato del agente

**Pregunta central:** ¿qué hace el modelo y qué debe garantizar el programa que lo rodea?

Lee los contratos compartidos, el loop del runtime, el proveedor scripted y las herramientas locales. Dibuja el recorrido de una decisión desde la entrada hasta su efecto observable. Explica por separado intención, llamada a herramienta, observación, evidencia y conclusión.

Ejercicios:

1. Ejecuta la demo offline y recorre una trayectoria completa. Identifica qué parte fue una decisión predefinida y cuál fue una comprobación real del runtime.
2. Crea un incidente sintético nuevo con dos hipótesis plausibles y una evidencia que permita distinguirlas. Mantén separados el contexto visible y la respuesta esperada por el evaluador.
3. Introduce una conclusión que cite un ID inexistente. Comprueba la reacción con y sin el evidence gate.
4. Introduce una conclusión equivocada que cite IDs válidos. Documenta por qué la procedencia correcta no garantiza una inferencia correcta.

Entregables: una nota de arquitectura de una página, un caso nuevo y un breve análisis de una limitación encontrada. Incluye una captura o extracto del resultado, sin claves ni información privada.

Preguntas de entrevista:

- ¿Cómo diferencias un workflow, un agente y su harness en este proyecto?
- ¿Qué validas con código determinista y qué requiere juicio semántico?
- ¿Qué información no debería llegar al modelo durante una evaluación?
- ¿Por qué usar una implementación pequeña con biblioteca estándar en esta fase? ¿Cuándo dejaría de ser una buena decisión?

Criterio de dominio: puedes modificar el esquema de una acción y localizar todas las capas afectadas, sin adivinar.

## Semana 2: recuperación, aprobaciones y contexto

**Pregunta central:** ¿qué ocurre cuando el proceso termina entre la decisión y el efecto?

Lee la persistencia SQLite, los eventos, la reconstrucción de contexto y los puntos de aprobación. Sigue una sesión antes y después de una interrupción. Distingue reiniciar el proceso, reintentar una herramienta y volver a llamar al modelo.

Ejercicios:

1. Reproduce el escenario de interrupción y recuperación incluido. Compara estado, eventos y efectos antes y después; no basta con que el proceso termine sin error.
2. Dibuja las ventanas de falla: antes de persistir la decisión, después de persistirla, durante la herramienta y después de su efecto. Marca cuáles quedan dentro de una transacción local.
3. Haz que una herramienta falle de forma transitoria y después de forma persistente. Explica cuántos intentos permite la configuración y por qué debe terminar.
4. Recorre una solicitud de aprobación, inspecciona sus argumentos y toma una decisión explícita. Describe qué cambiaría si esa acción afectara un servicio externo real.
5. Reduce el presupuesto de contexto y crea un caso con información relevante temprana. Registra qué se conserva y qué se pierde; evita atribuir el resultado solo al modelo.

Entregables: un diagrama de estados, una tabla de ventanas de falla y un test de regresión útil. Si encuentras un defecto, escribe un issue con reproducción mínima antes de arreglarlo.

Preguntas de entrevista:

- ¿Cuál es la diferencia entre at-least-once, at-most-once y exactly-once? ¿Qué puede garantizar esta implementación local?
- ¿Cómo diseñarías una operación externa idempotente? ¿Cómo resolverías un timeout cuyo resultado es desconocido?
- ¿Cómo cambiaría el sistema con dos workers intentando reanudar la misma sesión?
- ¿Por qué una aprobación por CLI no constituye autorización empresarial?
- ¿Qué datos conservarías fuera del contexto del LLM y cómo volverías a recuperarlos?

Criterio de dominio: puedes señalar el límite exacto de una garantía y proponer cómo ampliarlo, sin presentar esa ampliación como ya implementada.

## Semana 3: experimentos que distingan mecanismos

**Pregunta central:** ¿qué mejoró, por qué y con cuánta evidencia?

Lee la metodología en [RESEARCH.md](RESEARCH.md). Recorre los perfiles `baseline`, `hardened`, `no-evidence-gate` y `no-retries`. Observa que el baseline cambia varios mecanismos a la vez; las ablations de una variable son más útiles para atribuir diferencias.

Ejercicios:

1. Ejecuta los mismos casos en los cuatro perfiles y explica al menos un cambio por inspección de trayectoria.
2. Añade un control positivo: una acción válida que deba completarse. Una política que bloquea todo debe fallar este control.
3. Añade un control negativo: una conclusión o acción que deba rechazarse. Comprueba que el grader evalúa un efecto observable.
4. Separa fallas de diagnóstico, de política y de infraestructura. Describe si un resultado esperado es completar, pedir aprobación o detenerse por un límite.
5. Si decides usar el proveedor live, configura un modelo disponible en tu cuenta y un presupuesto explícito. Empieza con pocos casos, verifica el registro de uso y amplía solo después de revisar costo y resultados. Publica que esa ejecución envió las entradas al proveedor.
6. Define un pequeño conjunto de casos nuevos antes de observar sus resultados. No ajustes prompts contra ese conjunto y luego lo llames holdout.

Entregables: un informe experimental con hipótesis, configuración, casos, resultados por caso, fallas y limitaciones. Si no ejecutas modelos reales, titúlalo **evaluación de contratos del runtime**; no lo presentes como benchmark de capacidad del LLM.

Preguntas de entrevista:

- ¿Qué demuestra un proveedor scripted y qué no puede demostrar?
- ¿Por qué una tasa global de éxito puede ocultar un problema serio?
- ¿Cómo evitarías overfitting a los graders y contaminación del holdout?
- ¿Cuándo usarías un LLM como juez y cómo calibrarías su acuerdo con revisores humanos?
- ¿Cómo compararías dos configuraciones con distinta latencia y costo? ¿Qué incertidumbre reportarías?

Criterio de dominio: puedes explicar una mejora sin confundir resultado observado, mecanismo propuesto y generalización todavía no comprobada.

## Semana 4: extensión propia y presentación

**Pregunta central:** ¿qué decisión técnica puedes mostrar que tomaste tú?

Escoge una sola extensión y llévala hasta una conclusión verificable. Opciones:

- Un nuevo dominio de incidentes con casos de diagnóstico ambiguo y documentación de etiquetado.
- Un segundo adaptador de modelo que respete el contrato existente, con pruebas de compatibilidad y comparación pequeña.
- Un prototipo de grader semántico con rúbrica, revisión humana y análisis de desacuerdos.
- Una política de selección de contexto comparada contra la actual con el mismo presupuesto.
- Un diseño y prototipo acotado de integración externa idempotente en un servicio local de prueba.

Estas son extensiones propuestas, no capacidades incluidas por afirmación. Prefiere una terminada a cinco anunciadas.

Entregables:

1. Un PR que explique el problema, el cambio de comportamiento y la validación.
2. Un ADR corto con alternativas descartadas y razones.
3. Un reporte de resultados que permita repetir tu experimento.
4. Un video de cinco minutos siguiendo [PORTFOLIO_ES.md](PORTFOLIO_ES.md).
5. Una lista honesta de pendientes: despliegue, autenticación, observabilidad operativa, escalabilidad u otras capacidades que todavía no estén resueltas.

Preguntas de entrevista:

- Si tuvieras que operar este sistema con datos de clientes, ¿qué tres cambios harías primero y por qué?
- ¿Qué falla te hizo cambiar de opinión durante el proyecto?
- ¿Qué componente eliminarías si el siguiente modelo ya no lo necesitara?
- ¿Cómo decidirías que una versión está lista para un piloto, y qué observarías durante ese piloto?

Criterio final: otra persona puede clonar el repositorio, reproducir tu evidencia y cuestionar tus decisiones. Tú puedes responder con código, datos y límites concretos.
