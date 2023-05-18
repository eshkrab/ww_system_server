# Use an official Python runtime as a parent image
FROM python:3.9-slim-buster

# Set the working directory in the container to /app
WORKDIR /app

# Add a new group and user
RUN groupadd -g 20 mygroup && useradd -u 502 -g mygroup wwuser

# Switch to the new user
USER wwuser

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Run server.py when the container launches
CMD ["python", "-m", "quart", "run", "-h", "0.0.0.0"]

