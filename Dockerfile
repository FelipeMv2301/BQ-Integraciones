FROM python:3.12-slim

WORKDIR /app

# UV: gestor de dependencias — se copia el binario directo desde su propia
# imagen oficial, sin pasar por pip para instalar el instalador.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Instalar dependencias ANTES de copiar el código: Docker cachea esta capa
# mientras pyproject.toml/uv.lock no cambien.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev

# Código fuente, en capas separadas.
COPY app/         ./app/
COPY alembic/     ./alembic/
COPY alembic.ini  ./

# Usuario sin privilegios de root dentro del contenedor.
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Comando por defecto: Web Service (FastAPI). docker-compose.yml sobreescribe
# esto con su propio "command:" para los servicios worker/beat.
# Forma shell (no exec) a propósito: expande ${PORT:-8000} si algún día algo
# de esto corre en Railway; para nuestro servidor, compose fija su propio
# puerto igual, así que no cuesta nada dejarlo preparado.
CMD uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
