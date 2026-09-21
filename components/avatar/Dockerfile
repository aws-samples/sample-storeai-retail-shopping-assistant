FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY liveavatar_server.py liveavatar_avatar.html ./
EXPOSE 8502
USER 10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8502), timeout=3)"]
CMD ["python", "liveavatar_server.py"]
