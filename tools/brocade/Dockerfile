FROM python:3.12-slim
WORKDIR /simulator
COPY simulated_api.py .
USER 65534:65534
EXPOSE 8080
CMD ["python", "simulated_api.py", "--host", "0.0.0.0", "--port", "8080"]
