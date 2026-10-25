# S MiniGPT: Local AI Model Roadmap

**Project owner:** S  
**Target machine:** Dell 15, Intel Core Ultra 5 225H, 16 GB RAM, 512 GB SSD, integrated Intel graphics, Windows 11  
**Primary goal:** Understand how language models work by training a small GPT-style transformer locally, then explore fine-tuning an existing open-weight model.

## Guiding principles

- Build in small, testable stages. Do not skip ahead when a stage fails.
- Start with a tiny model that is practical on CPU. This is an educational project, not a ChatGPT-scale training run.
- No external model API is required for the from-scratch training stages.
- Keep the existing Friday assistant unchanged until the local model is tested independently.
- Use checkpoints, fixed random seeds, clear logs, and reproducible commands.
- Never put API keys, passwords, personal messages, or private data into the training dataset.

## Current status

- [ ] Fashion-MNIST PyTorch CPU experiment running.
- [ ] Record final training/validation loss, accuracy, runtime, and any errors.
- [ ] Begin MiniGPT only after the Fashion-MNIST run finishes and its results are recorded.

Fashion-MNIST is an image-classification task. Its accuracy does **not** predict language-model quality; it is a separate PyTorch and training-pipeline exercise.

## Stage 0: Inspect the machine and current environment

Before changing anything, inspect the existing project and Python environment.

1. Record Windows version, Python version, PyTorch version, available RAM, and free disk space.
2. Find the Fashion-MNIST script, its configuration, logs, and output/checkpoint files.
3. Confirm whether a virtual environment already exists. Do not overwrite or delete it.
4. Check whether any training process is still running.
5. Save the inspection results to `reports/environment.md`.

**Acceptance criteria**
- The current environment is documented.
- No existing files, environments, or model checkpoints have been deleted or overwritten.
- The Fashion-MNIST run has either completed or is clearly identified as still running.

## Stage 1: Finish and document Fashion-MNIST

Let the current job finish unless it is clearly stuck or failing.

Record:
- Training and validation loss
- Training and validation accuracy
- Total runtime
- Epoch count and batch size
- CPU and memory behavior, if available
- Any warnings or errors

Save a short report to `reports/fashion-mnist-results.md`. Do not chase a particular accuracy number by silently changing the experiment. If results are poor, investigate the configuration and data pipeline first.

**Acceptance criteria**
- Results and the exact command/configuration are recorded.
- The model checkpoint, if one is produced, is preserved.
- The run can be repeated using documented steps.

## Stage 2: Create a tiny text-generation baseline

Build a minimal character-level language model before implementing a transformer. This creates a baseline and makes the later transformer easier to debug.

1. Use a small, public-domain text corpus or a tiny synthetic text file.
2. Split the data into training and validation sets.
3. Build a character vocabulary and encode/decode functions.
4. Train a small next-character predictor.
5. Generate text from a prompt and report training/validation loss.

**Suggested scale**
- Small corpus: roughly 100 KB to a few MB
- Embedding/hidden width: 64
- Batch size: 16 or 32
- Short training runs first; increase only after measuring CPU runtime

**Acceptance criteria**
- Encoding and decoding round-trip correctly.
- Training loss decreases on a small test run.
- The script generates text and saves a checkpoint.
- The report explains that generated text may be repetitive or incoherent.

## Stage 3: Implement S MiniGPT, a tiny decoder-only transformer

Use **PyTorch** and implement the essential pieces explicitly for learning.

Components:
- Tokenizer: start with character-level tokenization; consider a subword tokenizer later.
- Token and positional embeddings
- Causal self-attention, which prevents looking at future tokens
- Feed-forward network
- Residual connections and layer normalization
- Next-token prediction with cross-entropy loss
- Training loop, validation loop, checkpoint saving, and text generation

**CPU-friendly starting configuration**
- Layers: 2
- Attention heads: 2
- Embedding width: 64
- Context length: 128 tokens
- Batch size: 8 to 16
- Training steps: begin with 500 to 2,000, then adjust based on measured speed
- Optimizer: AdamW
- Fixed random seed for repeatability

These are starting values, not guarantees. CPU runtime can range from minutes to much longer depending on corpus, implementation, and system load. Keep the first run small and measure it before scaling.

**Required safeguards**
- Check tensor shapes and causal masking.
- Ensure validation data is not used for gradient updates.
- Clip gradients if training becomes unstable.
- Save checkpoints periodically.
- Stop with a clear error if loss becomes NaN or infinite.
- Do not load untrusted serialized model files.

**Acceptance criteria**
- A unit test confirms that the causal mask blocks future-token attention.
- A tiny overfit test can drive loss down on a very small sample.
- Training and validation loss are logged.
- Text generation works from a prompt.
- A checkpoint can be loaded in a fresh process and generate text again.

## Stage 4: Evaluate and improve carefully

Run controlled experiments, changing one variable at a time.

Possible experiments:
- Context length
- Embedding width and layer count
- Training steps
- Learning rate
- Dataset size and quality
- Character-level versus subword tokenization

Track each experiment in `reports/experiments.csv` with the configuration, runtime, final losses, and notes.

**Important:** A tiny model trained on a small corpus is mainly for learning. It will not have the general reasoning, factual knowledge, instruction-following, or reliability of ChatGPT or Claude.

## Stage 5: Package the local model

Create a simple command-line interface that:
- Loads a checkpoint
- Accepts a text prompt
- Generates a configurable number of tokens
- Supports temperature and top-k sampling
- Prints useful errors when files or arguments are missing

Keep inference separate from training so the model can be tested without retraining.

**Acceptance criteria**
- One documented command starts text generation.
- Inference works after restarting the computer.
- The model runs fully offline after dependencies and data are present.

## Stage 6: Explore fine-tuning an open-weight model

Only after the tiny transformer works, investigate fine-tuning a small existing model.

1. Compare model licenses and intended use before choosing a base model.
2. Check model size, RAM needs, Windows compatibility, and CPU inference speed.
3. Prefer a small quantized model for local inference.
4. For training, investigate parameter-efficient fine-tuning such as LoRA/QLoRA, but verify hardware and library compatibility first.
5. Use a small, clean, permissioned dataset and hold out evaluation examples.
6. Do not assume the 16 GB CPU-only laptop can efficiently fine-tune a multi-billion-parameter model. Cloud GPU rental may be more practical for that stage.

This stage is a separate project and must not replace or destabilize the working tiny model.

## Stage 7: Consider connecting it to Friday

Do this only after standalone inference is reliable.

- Keep Friday's current model providers and tools intact.
- Add the local model as an optional provider behind a feature flag.
- Use a narrow adapter/interface so the provider can be swapped safely.
- Test timeouts, malformed output, context limits, and failures.
- Do not give the experimental model direct access to sensitive tools or actions.
- Require explicit confirmation for consequential actions such as sending messages, deleting files, purchases, or changing system settings.
- Log failures without storing secrets or unnecessary private content.
- Keep a simple rollback path to the existing provider.

A tiny model is initially best treated as an educational text generator, not as Friday's primary reasoning model.

## Suggested project structure

```text
s-minigpt/
├── README.md
├── requirements.txt
├── configs/
│   └── tiny_cpu.json
├── data/
│   └── README.md
├── src/
│   ├── data.py
│   ├── tokenizer.py
│   ├── baseline.py
│   ├── model.py
│   ├── train.py
│   └── generate.py
├── tests/
│   ├── test_tokenizer.py
│   └── test_attention_mask.py
├── checkpoints/
│   └── .gitkeep
├── reports/
│   └── .gitkeep
└── logs/
    └── .gitkeep
```

Do not commit datasets, checkpoints, `.env` files, API keys, or personal data to Git. Add appropriate entries to `.gitignore`.

## Libraries

Keep the first version lean:
- `torch` for tensor operations and model training
- Python standard library for basic text handling, logging, JSON, and command-line arguments
- `pytest` for tests (optional but recommended)
- `tiktoken` or a Hugging Face tokenizer only if/when experimenting with tokenization beyond characters

Do not add a large framework unless the current stage genuinely needs it.

## Instructions for Claude Code

Work one stage at a time. First inspect the repository and current Fashion-MNIST job; do not immediately generate the entire project or modify existing files.

For each stage:
1. Explain the intended changes briefly.
2. Inspect relevant files before editing.
3. Make the smallest useful implementation.
4. Run tests and a short CPU smoke test.
5. Report changed files, exact commands, results, runtime, and any limitations.
6. Update the README and relevant report.
7. Stop at the stage boundary and wait for approval before starting the next stage.

Do not claim a test passed unless it was actually run. Do not silently install packages, overwrite files, terminate processes, or change the existing Friday assistant. Ask first before destructive or system-wide actions.

## First task for Claude Code

**Inspect and report only.** Find the active Fashion-MNIST job and its project files, determine whether it is still running, inspect the environment, and identify where its results will be saved. Do not stop the process or modify files. Then provide the commands and metrics needed to mark Stage 1 complete.
