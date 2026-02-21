#!/usr/bin/env bash

# Wrapper script to run the benchmarks
# It will use the current python environment and execute the module

python3 -m llm_benchmarks "$@"
