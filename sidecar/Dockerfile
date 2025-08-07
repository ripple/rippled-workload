FROM docker.io/python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the application files into the container
COPY . /app

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# # Command to run the application (modify as needed)
# ENTRYPOINT ["python", "sidecar.py", "-t", "1"]
