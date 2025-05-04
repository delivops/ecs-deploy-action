# Use official Python image as base
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Default command (override as needed)
CMD ["python", "scripts/generate_task_def.py", "--help"]
