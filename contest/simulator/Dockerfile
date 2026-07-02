FROM python:3.11-slim

WORKDIR /app

# Copy simulator code
COPY simulator/ /app/simulator/
COPY cases/ /app/cases/
COPY tool_specs.json /app/tool_specs.json
COPY data/ /app/data/
COPY requirements.base.txt /tmp/requirements.base.txt

# Install preapproved dependencies used by contestants and the simulator runtime
RUN pip install --no-cache-dir -r /tmp/requirements.base.txt

# Set PYTHONPATH
ENV PYTHONPATH=/app/simulator

# Default command: run baseline agent
CMD ["python", "simulator/baseline_agent.py"]
