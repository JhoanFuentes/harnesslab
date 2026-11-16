# Publicar en GitHub

El proyecto está preparado para publicación, pero no se ha subido a ninguna
cuenta. La carpeta que debes usar como raíz del repositorio es `harnesslab`:
contiene `README.md`, `pyproject.toml`, `.github`, `harnesslab/` y `tests/`.

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
3.13. Esa matriz no se ejecutó en GitHub durante la construcción local.

Descripción sugerida:

> Durable agent runtime and behavioral evaluation lab: evidence gates, crash recovery, scoped approvals and reproducible ablations.

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
