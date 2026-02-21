# LLM Benchmark Generator

This tool allows you to benchmark various Large Language Models (LLMs) via the OpenRouter API. It generates code based on a user prompt using multiple models in parallel and saves the results for comparison, while also tracking lifetime performance analytics.

## Prerequisites

- Python 3.8+
- An OpenRouter API key

## Installation

1. Clone the repository (if you haven't already):
   ```bash
   git clone <repository-url>
   cd llm-benchmark
   ```

2. Create a virtual environment (optional but recommended):
   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS/Linux
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   # Or install as a package:
   pip install .
   ```

## Configuration

You need to provide your OpenRouter API key. You can do this in one of two ways:

1. **Environment Variable**: Set `OPENROUTER_API_KEY` in your environment.
2. **.env File**: Create a `.env` file in the project root containing:
   ```
   OPENROUTER_API_KEY=your_key_here
   ```

## Usage

You can run the benchmark using the provided shell script or as a Python module:

```bash
./run_benchmarks.sh
# OR
python -m llm_benchmarks
```

You will be prompted to select a mode (Code or Text) and to enter a description of the prompt you want the LLMs to process.

Alternatively, you can provide the prompt directly as an argument:

```bash
./run_benchmarks.sh --prompt "Create a snake game in Python"
```

To run in text mode (for prose answers instead of code):
```bash
./run_benchmarks.sh --prompt "Explain quantum computing" --text
```

### Configuration Menu

You can interactively configure which models to benchmark and view their pricing:

```bash
./run_benchmarks.sh --config
```
*(Or press `c` during the interactive prompt)*

### Analytics

The tool tracks lifetime analytics of your model runs. To view them:

```bash
./run_benchmarks.sh --stats
```

To clear your history:

```bash
./run_benchmarks.sh --clear-history
```

## Output

The script will:
1. Generate a directory name based on your prompt using a fast model.
2. Create a folder in `benchmarkResults/`.
3. Query multiple models concurrently.
4. Extract the code from the response.
5. Save the generated code (or text) from each model into the folder.

## Models

The script evaluates a variety of models. You can interactively select models using the `--config` menu, which fetches the top models from OpenRouter.

The default active models are defined in `MODEL_MAPPING` located in `llm_benchmarks/models.py`. You can modify this dictionary to change the default models.