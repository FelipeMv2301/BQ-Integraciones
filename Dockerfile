FROM python:3.12-slim

WORKDIR /app

# UV: gestor de dependencias — se copia el binario directo desde su propia
# imagen oficial, sin pasar por pip para instalar el instalador.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Usuario sin privilegios de root — creado ANTES de instalar dependencias,
# nunca depende del código de la app (cachea junto con la capa de abajo).
RUN useradd --create-home --shell /bin/bash app

# Instalar dependencias ANTES de copiar el código: Docker cachea esta capa
# mientras pyproject.toml/uv.lock no cambien. chown acá (una sola vez, sobre
# el .venv con miles de archivos de site-packages) en vez de un "RUN chown -R"
# aparte después de copiar el código -- ese chown recursivo se re-ejecutaba
# en CADA deploy (cualquier cambio de app/ invalida esta capa en el
# Dockerfile original), tardando minutos por recorrer todo /app de nuevo.
# Hallazgo real, 2026-09-03: comparado en vivo, build+up con todo cacheado
# tarda ~50s; con un solo archivo de app/ tocado, el chown -R solo ya
# llevaba 3+ min sin terminar.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev && chown -R app:app /app

# Código fuente — chown acá mismo (COPY --chown), barato: son los pocos
# archivos de la app, no el .venv entero.
COPY --chown=app:app app/         ./app/
COPY --chown=app:app alembic/     ./alembic/
COPY --chown=app:app alembic.ini  ./

USER app

EXPOSE 8000

# Comando por defecto: Web Service (FastAPI). docker-compose.yml sobreescribe
# esto con su propio "command:" para los servicios worker/beat.
# Forma shell (no exec) a propósito: expande ${PORT:-8000} si algún día algo
# de esto corre en Railway; para nuestro servidor, compose fija su propio
# puerto igual, así que no cuesta nada dejarlo preparado.
CMD uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
