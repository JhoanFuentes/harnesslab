# Publicación y mantenimiento en GitHub

El proyecto está publicado en [JhoanFuentes/harnesslab](https://github.com/JhoanFuentes/harnesslab),
con [reporte navegable](https://jhoanfuentes.github.io/harnesslab/) y
[releases](https://github.com/JhoanFuentes/harnesslab/releases).
La ampliación v0.2.0 añade una [vista previa del estudio](https://jhoanfuentes.github.io/harnesslab/study/)
sin resultados de modelos reales; las ejecuciones con API siguen pendientes.
Los comandos de inicialización siguientes son para publicar otra copia o un fork,
no para reinicializar el repositorio existente.

Para mantener esta publicación, ejecuta las pruebas y
`python scripts/verify_example.py` antes de enviar cambios. Si cambias el runtime,
genera un experimento nuevo en `runs/review`, revisa sus resultados y actualiza
conjuntamente los nuevos artefactos cuando corresponda. La corrida histórica
v0.1.0 conserva su snapshot de fuente y sus resultados originales. No presentes
una corrida nueva como si fuera la anterior. Los planes nuevos se guardan en un
directorio de versión y deben tener un hash del runtime vigente.

`python scripts/build_pages.py --output runs/public-review` valida y prepara
solamente cinco archivos públicos revisados: reporte/JSON/SQLite histórico y
HTML/JSON del plan actual. Nunca copia `runs/` ni claves al sitio.

## Antes del primer commit

Ejecuta desde esa carpeta:

```sh
python -m unittest discover -s tests -v
python -m harnesslab demo --output runs/prepublish
git init -b main
git add .
git diff --cached --stat
git status --short
```

Revisa los archivos preparados. `.gitignore` excluye claves, cachés y sesiones de
trabajo. La excepción `examples/demo/traces-*.sqlite` incluye únicamente la
corrida sintética revisada que acompaña al reporte. No guardes allí datos reales.
No es necesario subir `runs/` ni tu archivo `.env`.

```sh
git commit -m "Build durable agent harness and reproducible behavioral evaluations"
```

Si Git solicita nombre o correo, configura tu identidad elegida antes de seguir;
no uses una identidad inventada. Puedes usar la dirección privada que proporcione
tu cuenta de GitHub.

## Crear el remoto

En GitHub crea un repositorio vacío llamado `harnesslab` (o el nombre que prefieras),
sin inicializar un segundo README. Copia su URL y úsala en lugar del ejemplo:

```sh
git remote add origin https://github.com/TU_USUARIO/harnesslab.git
git push -u origin main
```

Completa la autenticación de GitHub por sus mecanismos habituales. No pongas
tokens en la URL ni dentro de archivos del proyecto. Comprueba el resultado de
GitHub Actions; la matriz configurada prueba Ubuntu y Windows con Python 3.11 y
3.13. La matriz inicial pasó en GitHub; el [registro de validación](VALIDATION.md)
enlaza las ejecuciones observadas. Comprueba el estado del commit que publiques.

Descripción sugerida:

> Durable AI agent runtime with repeated-trial evaluations, failure-inclusive cost per successful task, and auditable execution traces.

Temas sugeridos: `ai-engineering`, `llm-agents`, `harness-engineering`, `evals`,
`python`, `agent-reliability`. Ajusta el nombre del paquete si eliges publicarlo
en un registro de paquetes; este proyecto no reserva el nombre en PyPI.

## Presentación

- Fija el repositorio en tu perfil y enlaza el README desde tu CV.
- Graba la [demo de cinco minutos](PORTFOLIO_ES.md) con una ejecución real.
- Presenta el reporte offline como prueba de contratos, y separa cualquier
  resultado live futuro por modelo, configuración, datos y fecha.
- Abre un issue para tu extensión y resuélvelo mediante un PR revisable.
- Publica una release cuando los comandos funcionen también en un clon limpio.

No fabriques historial de commits, estrellas, usuarios o métricas. Una pequeña
contribución original bien evaluada vale más como evidencia que afirmaciones
amplias que no puedas reproducir.
