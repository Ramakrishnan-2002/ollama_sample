# ==========================================
# STAGE 1: The Builder
# ==========================================
FROM python:3.11-slim as builder

# Stop Python from writing .pyc files and buffer stdout for clean logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Create a virtual environment (this makes it easy to copy dependencies later)
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy and install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ==========================================
# STAGE 2: The Runner (Final Image)
# ==========================================
FROM python:3.11-slim

# Copy the environment variables to this stage too
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# SECURITY: Create a non-root user and group
RUN addgroup --system appgroup && adduser --system --group appuser

WORKDIR /app

# MULTI-STAGE MAGIC: Copy ONLY the clean virtual environment from Stage 1
COPY --from=builder /opt/venv /opt/venv

# Copy your actual application code
COPY . .

# Change ownership of the files to our secure non-root user
RUN chown -R appuser:appgroup /app

# Switch to the secure user
USER appuser

# Expose the port
EXPOSE 8000

# Command to run the app
CMD ["uvicorn", "app.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]