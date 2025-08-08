# Use the latest Python runtime with security patches
FROM python:3.12.8-slim as backend-builder

# Install system dependencies and update packages for security patches
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container for the backend
WORKDIR /app/backend

# Copy the backend directory contents into the container at /app/backend
COPY ./backend /app/backend
RUN rm -f /app/backend/db/app.db

# Install any needed packages specified in backend/requirements_versioned.txt
RUN pip install --no-cache-dir -r requirements_versioned.txt

# Use latest Node.js official image for the frontend
FROM node:22-slim as frontend-builder

# Update system packages for security
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container for the frontend
WORKDIR /app

# Copy the VERSION and config file first so they can be used by Nuxt
COPY ./VERSION /app/VERSION
COPY ./bow-config.yaml /app/bow-config.yaml

# Copy the frontend directory contents
COPY ./frontend /app/frontend

# Set working directory for frontend
WORKDIR /app/frontend

# Install frontend dependencies and build the project
RUN yarn install
RUN yarn build

# Final image - use latest Python with security patches
FROM python:3.12.8-slim

# Install Node.js, update all packages, and clean up in one layer
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y curl libpq-dev gcc git && \
    curl -sL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    npm install --global yarn@latest && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

RUN mkdir -p /app/backend/db && \
    chown -R nobody:nogroup /app

# Copy backend and frontend builds from their respective builder images
COPY --from=backend-builder /app/backend /app/backend
COPY --from=frontend-builder /app/frontend /app/frontend

# Install Python dependencies globally
COPY ./backend/requirements_versioned.txt /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements_versioned.txt

# Set the working directory to /app for CMD execution
WORKDIR /app

COPY ./VERSION /app/VERSION
COPY ./start.sh /app/start.sh
COPY ./bow-config.yaml /app/bow-config.yaml

# Set executable permissions for start.sh
RUN chmod +x /app/start.sh

# Define environment variable for Node to run in production mode
ENV NODE_ENV=production
ENV ENVIRONMENT=production

# Add these before the CMD
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=UTF-8

# Expose ports (not required by Heroku, but useful for local development)
EXPOSE 3000

CMD ["/bin/bash", "start.sh"]