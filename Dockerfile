FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY obs_api ./obs_api
# usuário sem privilégios; /data é o volume do SQLite
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /data && chown 10001:10001 /data
USER 10001
ENV DB_PATH=/data/obs.db
EXPOSE 8000
CMD ["uvicorn", "--factory", "obs_api.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
