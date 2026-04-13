# Contributing

Dev setup and pipeline internals for anyone working on the codebase.

## Setting Up the Development Environment

### Prerequisites

- [conda](https://docs.conda.io/en/latest/) (Anaconda or Miniconda)
- A Nebius account with an API key ([nebius.com](https://nebius.com))

### Installation

```bash
# 1. Clone the repository
git clone <repo-url>
cd tpi-rag-decomposition

# 2. Create and activate the conda environment
conda env create -f environment.yml
conda activate rag

# 3. Set up environment variables
cp .env.example .env
# Then open .env and fill in your NEBIUS_API_KEY
```

### Getting a Nebius API Key

1. Sign in at [nebius.com](https://nebius.com) and open your project.
2. Navigate to **API Keys** and create a new key.
3. Copy the key into your `.env` file:
   ```
   NEBIUS_API_KEY=<your key here>
   ```

Your `.env` file is git-ignored — never commit it.

## How the Pipeline Works

> **TODO**: Describe the internal architecture of the data pipeline:
>
> - What are the main stages (ingestion, chunking, retrieval, generation, evaluation)?
> - Which modules or scripts are responsible for each stage?
> - How does data flow between the decomposition pipeline and the single-shot baseline?
> - How are intermediates persisted to SQLite?
> - Any key design decisions worth explaining.

## Known Bugs / Areas for Improvement

> **TODO**: List any known bugs or limitations that contributors may want to tackle. For example:
>
> - [ ] Bug: ...
> - [ ] Improvement: ...
> - [ ] Tech debt: ...

## Running Tests

> **TODO**: Describe how to run the test suite.

```bash
pytest
```

## Code Style and Guidelines

> **TODO**: Document any code style conventions, linting tools, or contribution guidelines (e.g. formatting with `black`, type annotations, commit message conventions).
