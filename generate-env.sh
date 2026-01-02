#!/bin/bash
# Generate .env from .env.template with variable expansion
# This is mainly used to allow Docker Compose to read environment variables (expand path)

cd "$(dirname "$0")"
envsubst < .env.template > .env
echo "Generated .env file with expanded variables"
