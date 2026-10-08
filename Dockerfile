# CGRecruit: one image that builds the web app and serves it from the API.
#
#   Stage 1 (web):  npm ci + build the React app in frontend/
#   Stage 2 (app):  Python runtime; the build is copied to backend/static,
#                   which is where server.py serves the web app from.
#
# Railway builds this automatically (railway.toml). Locally:
#   docker build -t cgrecruit .
#   docker run --env-file backend/.env -p 8000:8000 cgrecruit

# ---------- Stage 1: web app ----------
FROM node:20-bookworm-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Optional branding/time-zone settings baked into the web app at build time.
# Railway passes service variables with these names in as build arguments.
ARG REACT_APP_BRAND_NAME=""
ARG REACT_APP_COMPANY_NAME=""
ARG REACT_APP_PRESENTATION_URL=""
ARG REACT_APP_TIMEZONE=""
# Empty backend URL = the app calls its own address (same origin).
ENV REACT_APP_BACKEND_URL="" \
    CI=false \
    GENERATE_SOURCEMAP=false
RUN npm run build

# ---------- Stage 2: API + static files ----------
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/ ./
COPY --from=web /web/build ./static
# Never bake a local .env or company secrets into the image.
RUN rm -f .env && useradd --create-home --uid 10001 cgr && chown -R cgr /app
USER cgr
EXPOSE 8000
# One process only: the scheduler and login throttle live in memory.
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
